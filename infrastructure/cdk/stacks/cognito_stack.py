"""Cognito User Pool for OAuth 2.0 (inbound service auth + outbound gateway auth).

Design
------
* One User Pool with a hosted domain (provides the /oauth2/token endpoint).
* A resource server exposing a single custom scope `<identifier>/invoke`.
* Two app clients, both using the client_credentials grant (machine-to-machine):

    - service-client   : the AgentCore Gateway OUTBOUND credential provider uses
                         this client to mint access tokens when calling
                         Services 1/2/3. The services validate that the token's
                         client_id matches this client (COGNITO_APP_CLIENT_ID).

    - gateway-client   : represents the MCP client (Amazon Quick / Kiro) calling
                         the gateway INBOUND. The gateway's JWT authorizer is
                         configured to allow this client id.

App client secrets are NOT emitted as CloudFormation outputs. deploy.sh reads
them at deploy time via `aws cognito-idp describe-user-pool-client` and passes
them straight into the credential-provider creation without logging them.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from constructs import Construct


class CognitoStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        project: str,
        domain_prefix: str,
        **kwargs,
    ):
        super().__init__(scope, construct_id, **kwargs)

        pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{project}-pool",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            self_sign_up_enabled=False,
        )

        # Hosted domain -> gives us https://<prefix>.auth.<region>.amazoncognito.com
        # CfnUserPoolDomain is used so a collision on the prefix surfaces clearly.
        domain = pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix),
        )

        # Resource server + custom scope used by the client_credentials grant.
        invoke_scope = cognito.ResourceServerScope(
            scope_name="invoke", scope_description="Invoke the demo enterprise services"
        )
        resource_server = pool.add_resource_server(
            "ResourceServer",
            identifier=project,
            scopes=[invoke_scope],
        )
        full_scope = f"{project}/invoke"

        common_oauth = cognito.OAuthSettings(
            flows=cognito.OAuthFlows(client_credentials=True),
            scopes=[cognito.OAuthScope.resource_server(resource_server, invoke_scope)],
        )

        service_client = pool.add_client(
            "ServiceClient",
            user_pool_client_name=f"{project}-service-client",
            generate_secret=True,
            o_auth=common_oauth,
            auth_flows=cognito.AuthFlow(),  # no interactive flows; m2m only
        )

        gateway_client = pool.add_client(
            "GatewayClient",
            user_pool_client_name=f"{project}-gateway-client",
            generate_secret=True,
            o_auth=common_oauth,
            auth_flows=cognito.AuthFlow(),
        )

        # ---- Gateway execution role ----
        # The AgentCore Gateway assumes this role to read the OpenAPI spec from
        # S3 and to use credential providers in the token vault. Trust the
        # bedrock-agentcore service principal.
        gateway_role = iam.Role(
            self,
            "GatewayRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description="Execution role assumed by the AgentCore Gateway",
        )
        gateway_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[f"arn:aws:s3:::{project}-gateway-specs-*/*"],
            )
        )
        # Allow the gateway to use identity/token-vault credential providers.
        gateway_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:GetResourceOauth2Token",
                    "bedrock-agentcore:GetResourceApiKey",
                    "bedrock-agentcore:GetWorkloadAccessToken",
                ],
                resources=["*"],
            )
        )
        gateway_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{cdk.Stack.of(self).region}:"
                    f"{cdk.Stack.of(self).account}:secret:{project}/*"
                ],
            )
        )

        # ---- Outputs consumed by deploy.sh / gateway setup ----
        region = cdk.Stack.of(self).region
        discovery_url = (
            f"https://cognito-idp.{region}.amazonaws.com/{pool.user_pool_id}"
            "/.well-known/openid-configuration"
        )
        token_url = f"{domain.base_url()}/oauth2/token"

        cdk.CfnOutput(self, "UserPoolId", value=pool.user_pool_id)
        cdk.CfnOutput(self, "CognitoDiscoveryUrl", value=discovery_url)
        cdk.CfnOutput(self, "CognitoTokenUrl", value=token_url)
        cdk.CfnOutput(self, "CognitoScope", value=full_scope)
        cdk.CfnOutput(self, "ServiceClientId", value=service_client.user_pool_client_id)
        cdk.CfnOutput(self, "GatewayClientId", value=gateway_client.user_pool_client_id)
        cdk.CfnOutput(self, "GatewayRoleArn", value=gateway_role.role_arn)
