"""ECR repositories - one per container service."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ecr as ecr
from constructs import Construct


class EcrStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, *, repo_names: list[str], **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        self.repositories: dict[str, ecr.Repository] = {}
        for name in repo_names:
            # CDK logical id cannot contain '/'; derive a safe id from the name.
            logical = "Repo" + "".join(p.capitalize() for p in name.replace("/", "-").split("-"))
            repo = ecr.Repository(
                self,
                logical,
                repository_name=name,
                image_scan_on_push=True,
                # Demo repos are disposable; empty + delete on `cdk destroy`.
                removal_policy=cdk.RemovalPolicy.DESTROY,
                empty_on_delete=True,
                lifecycle_rules=[
                    ecr.LifecycleRule(
                        description="Keep only the last 5 images",
                        max_image_count=5,
                    )
                ],
            )
            self.repositories[name] = repo
            cdk.CfnOutput(self, f"{logical}Uri", value=repo.repository_uri, export_name=f"{construct_id}-{logical}-uri")
