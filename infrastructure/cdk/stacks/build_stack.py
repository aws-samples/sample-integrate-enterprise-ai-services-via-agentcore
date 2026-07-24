"""CodeBuild-based container image builder.

Images are built on AWS CodeBuild (native linux/amd64, Docker preinstalled)
rather than on the operator's machine, so `deploy.sh` needs no local Docker and
no cross-architecture emulation. deploy.sh zips each service directory, uploads
it to the source bucket created here, and starts a build; the project's inline
buildspec logs in to ECR, builds the image, and pushes it.

One reusable project handles all services: deploy.sh overrides the S3 source
location and the REPO / IMAGE_TAG environment variables per build.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct


class BuildStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        project: str,
        repo_names: list[str],
        **kwargs,
    ):
        super().__init__(scope, construct_id, **kwargs)

        region = cdk.Stack.of(self).region
        account = cdk.Stack.of(self).account
        registry = f"{account}.dkr.ecr.{region}.amazonaws.com"

        # Bucket that deploy.sh uploads the per-service build contexts (zips) to.
        source_bucket = s3.Bucket(
            self,
            "BuildSource",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            enforce_ssl=True,
        )

        # Inline buildspec: ECR login, build, push. The build context (Dockerfile
        # + app) is the unzipped S3 source, so `docker build .` uses the repo root.
        build_spec = codebuild.BuildSpec.from_object(
            {
                "version": "0.2",
                "phases": {
                    "pre_build": {
                        "commands": [
                            "echo Logging in to ECR $ECR_REGISTRY",
                            "aws ecr get-login-password --region $AWS_REGION | "
                            "docker login --username AWS --password-stdin $ECR_REGISTRY",
                        ]
                    },
                    "build": {
                        "commands": [
                            "echo Building $ECR_REGISTRY/$REPO:$IMAGE_TAG",
                            "docker build -t $ECR_REGISTRY/$REPO:$IMAGE_TAG .",
                        ]
                    },
                    "post_build": {
                        "commands": [
                            "echo Pushing $ECR_REGISTRY/$REPO:$IMAGE_TAG",
                            "docker push $ECR_REGISTRY/$REPO:$IMAGE_TAG",
                        ]
                    },
                },
            }
        )

        project_build = codebuild.Project(
            self,
            "ImageBuilder",
            project_name=f"{project}-image-builder",
            # A default S3 source; deploy.sh overrides the exact object per build.
            source=codebuild.Source.s3(
                bucket=source_bucket, path="sources/placeholder.zip"
            ),
            environment=codebuild.BuildEnvironment(
                # STANDARD_7_0 is a native linux/amd64 image with Docker; EKS
                # nodes are amd64 so no cross-build/emulation is needed.
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                compute_type=codebuild.ComputeType.SMALL,
                privileged=True,  # required to run the Docker daemon
            ),
            environment_variables={
                "AWS_REGION": codebuild.BuildEnvironmentVariable(value=region),
                "ECR_REGISTRY": codebuild.BuildEnvironmentVariable(value=registry),
                # Overridden per build by deploy.sh:
                "REPO": codebuild.BuildEnvironmentVariable(value=repo_names[0]),
                "IMAGE_TAG": codebuild.BuildEnvironmentVariable(value="latest"),
            },
            build_spec=build_spec,
            timeout=cdk.Duration.minutes(30),
        )

        source_bucket.grant_read(project_build)

        # Allow the build to authenticate to ECR and push to the demo repos only.
        project_build.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        project_build.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload",
                    "ecr:PutImage",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                ],
                resources=[
                    f"arn:aws:ecr:{region}:{account}:repository/{name}"
                    for name in repo_names
                ],
            )
        )

        cdk.CfnOutput(self, "BuildProjectName", value=project_build.project_name)
        cdk.CfnOutput(self, "BuildSourceBucket", value=source_bucket.bucket_name)
