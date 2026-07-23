"""EKS cluster + managed node group + ALB Controller + workload IRSA.

The cluster hosts the three demo services. The AWS Load Balancer Controller
turns their Ingress resources into internet-facing ALBs. A dedicated Kubernetes
service account (via IRSA) grants the pods permission to call Bedrock (Service 1
and Service 2 invoke Claude on Bedrock).
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_eks as eks
from aws_cdk import aws_iam as iam
from aws_cdk.lambda_layer_kubectl_v31 import KubectlV31Layer
from constructs import Construct

# Namespace and service account name the k8s manifests reference via placeholders.
K8S_NAMESPACE = "demo"
SERVICE_ACCOUNT_NAME = "demo-workload"


class EksStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        project: str,
        cluster_name: str,
        eks_version: str,
        node_instance_type: str,
        node_min: int,
        node_max: int,
        node_desired: int,
        **kwargs,
    ):
        super().__init__(scope, construct_id, **kwargs)

        vpc = ec2.Vpc(self, "Vpc", max_azs=2, nat_gateways=1)

        # Admin role that can run kubectl against the cluster (used by deploy.sh
        # after `aws eks update-kubeconfig`). Anyone assuming this role in the
        # account is granted cluster-admin via masters mapping.
        masters_role = iam.Role(
            self,
            "ClusterAdminRole",
            assumed_by=iam.AccountRootPrincipal(),
            description="Cluster admin (system:masters) for the AgentCore demo",
        )

        cluster = eks.Cluster(
            self,
            "Cluster",
            cluster_name=cluster_name,
            version=eks.KubernetesVersion.of(eks_version),
            kubectl_layer=KubectlV31Layer(self, "KubectlLayer"),
            default_capacity=0,  # we add an explicit managed node group below
            vpc=vpc,
            vpc_subnets=[ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)],
            masters_role=masters_role,
            endpoint_access=eks.EndpointAccess.PUBLIC_AND_PRIVATE,
            alb_controller=eks.AlbControllerOptions(
                version=eks.AlbControllerVersion.V2_8_2,
            ),
        )

        cluster.add_nodegroup_capacity(
            "NodeGroup",
            instance_types=[ec2.InstanceType(node_instance_type)],
            min_size=node_min,
            max_size=node_max,
            desired_size=node_desired,
            capacity_type=eks.CapacityType.ON_DEMAND,
        )

        # Namespace for the demo workloads.
        namespace = cluster.add_manifest(
            "DemoNamespace",
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": K8S_NAMESPACE},
            },
        )

        # IRSA service account: pods assume this role to call Bedrock.
        workload_sa = cluster.add_service_account(
            "WorkloadSA",
            name=SERVICE_ACCOUNT_NAME,
            namespace=K8S_NAMESPACE,
        )
        workload_sa.node.add_dependency(namespace)
        workload_sa.add_to_principal_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                # Scoped to foundation models + inference profiles; the specific
                # model id is chosen at runtime via BEDROCK_MODEL_ID.
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/*",
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*",
                    f"arn:aws:bedrock:*::foundation-model/*",
                ],
            )
        )

        # ---- Outputs ----
        cdk.CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        cdk.CfnOutput(self, "KubectlRoleArn", value=masters_role.role_arn)
        cdk.CfnOutput(self, "WorkloadNamespace", value=K8S_NAMESPACE)
        cdk.CfnOutput(self, "WorkloadServiceAccount", value=SERVICE_ACCOUNT_NAME)
        cdk.CfnOutput(
            self,
            "UpdateKubeconfigCommand",
            value=(
                f"aws eks update-kubeconfig --name {cluster.cluster_name} "
                f"--region {self.region} --role-arn {masters_role.role_arn}"
            ),
        )
