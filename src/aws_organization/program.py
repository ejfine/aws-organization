import logging
import time
from typing import Any
from typing import override

from ephemeral_pulumi_deploy import get_aws_account_id
from ephemeral_pulumi_deploy import get_config
from ephemeral_pulumi_deploy.utils import common_tags
from ephemeral_pulumi_deploy.utils import common_tags_native
from pulumi import ComponentResource
from pulumi import Output
from pulumi import ResourceOptions
from pulumi import export
from pulumi.dynamic import CreateResult
from pulumi.dynamic import Resource
from pulumi.dynamic import ResourceProvider
from pulumi_aws import identitystore as identitystore_classic
from pulumi_aws import ssoadmin
from pulumi_aws.iam import GetPolicyDocumentStatementArgs
from pulumi_aws.iam import GetPolicyDocumentStatementConditionArgs
from pulumi_aws.iam import GetPolicyDocumentStatementPrincipalArgs
from pulumi_aws.iam import get_policy_document
from pulumi_aws.organizations import get_organization
from pulumi_aws_native import Provider
from pulumi_aws_native import ProviderAssumeRoleArgs
from pulumi_aws_native import iam
from pulumi_aws_native import organizations
from pulumi_aws_native import s3
from pulumi_aws_native import ssm
from pulumi_command.local import Command

from .constants import CENTRAL_INFRA_GITHUB_ORG_NAME
from .constants import CENTRAL_INFRA_REPO_NAME
from .lib import WORKLOAD_INFO_SSM_PARAM_PREFIX
from .lib import AwsAccountInfo
from .lib import AwsLogicalWorkload

logger = logging.getLogger(__name__)

DEFAULT_ORG_ACCESS_ROLE_NAME = "OrganizationAccountAccessRole"


class SleepProvider(ResourceProvider):
    @override
    def create(self, props: dict[str, Any]) -> CreateResult:
        duration = props["seconds"]
        time.sleep(duration)
        return CreateResult(id_="sleep-done", outs={})


class Sleep(Resource):
    def __init__(self, name: str, seconds: float, opts: ResourceOptions | None = None):
        super().__init__(SleepProvider(), name, {"seconds": seconds}, opts)


class AwsSsoPermissionSet(ComponentResource):
    def __init__(
        self,
        name: str,
        description: str,
        managed_policies: list[str],
    ):
        super().__init__("labauto:AwsSsoPermissionSet", name, None)
        sso_instances = ssoadmin.get_instances()
        assert len(sso_instances.arns) == 1, "Expected a single AWS SSO instance to exist"
        sso_instance_arn = sso_instances.arns[0]
        self.name = name
        permission_set = ssoadmin.PermissionSet(
            name,
            instance_arn=sso_instance_arn,
            name=name,
            description=description,
            session_duration="PT12H",
            opts=ResourceOptions(parent=self),
        )
        self.permission_set_arn = permission_set.arn
        for policy_name in managed_policies:
            _ = ssoadmin.ManagedPolicyAttachment(
                f"{name}-{policy_name}",
                instance_arn=sso_instance_arn,
                managed_policy_arn=f"arn:aws:iam::aws:policy/{policy_name}",
                permission_set_arn=self.permission_set_arn,
                opts=ResourceOptions(parent=self),
            )
        self.register_outputs(
            {
                "permission_set_arn": self.permission_set_arn,
            }
        )


class AwsSsoPermissionSetAccountAssignments(ComponentResource):
    def __init__(
        self,
        *,
        account_id: Output[str],
        account_name: str,  # TODO: handle this being an Output
        permission_set: AwsSsoPermissionSet,
        users: list[str],
    ):
        resource_name = f"{permission_set.name}-{account_name}"
        super().__init__(
            "resilience-cloud:AwsSsoPermissionSetAccountAssignments",
            resource_name,
            None,
        )
        sso_instances = ssoadmin.get_instances()
        assert len(sso_instances.arns) == 1, "Expected a single AWS SSO instance to exist"
        sso_instance_arn = sso_instances.arns[0]
        self.identity_store_id = sso_instances.identity_store_ids[0]
        users = list(set(users))  # Remove any duplicates in the list

        for user in users:
            _ = ssoadmin.AccountAssignment(
                f"{resource_name}-{user}",
                instance_arn=sso_instance_arn,
                permission_set_arn=permission_set.permission_set_arn,
                principal_id=self.lookup_user_id(user),
                principal_type="USER",
                target_id=account_id,
                target_type="AWS_ACCOUNT",
                opts=ResourceOptions(parent=self),
            )

    def lookup_user_id(self, name: str) -> str:
        """Convert a username <first>.<last> name into an AWS SSO User ID."""
        return identitystore_classic.get_user(
            alternate_identifier=identitystore_classic.GetUserAlternateIdentifierArgs(
                unique_attribute=identitystore_classic.GetUserAlternateIdentifierUniqueAttributeArgs(
                    attribute_path="UserName", attribute_value=name
                )
            ),
            identity_store_id=self.identity_store_id,
        ).user_id


class AwsAccount(ComponentResource):
    def __init__(
        self,
        *,
        account_name: str,
        ou: organizations.OrganizationalUnit,
    ):
        super().__init__("labauto:aws-organization:AwsAccount", account_name, None)
        self.account = organizations.Account(
            account_name,
            opts=ResourceOptions(parent=self),
            account_name=account_name,
            email=f"ejfine+{account_name}@gmail.com",
            parent_ids=[ou.id],
            # Deliberately not setting the role_name here, as it causes problems during any subsequent updates, even when not actually changing the role name. Could possible set up ignore_changes...but just leaving it out for now
            tags=common_tags_native(),
        )
        self.wait_after_account_create = Sleep(
            f"wait-after-account-create-{account_name}",
            60,
            opts=ResourceOptions(parent=self, depends_on=[self.account]),
        )
        self.account_info_kwargs = self.account.id.apply(lambda account_id: {"id": account_id, "name": account_name})

        export(f"{account_name}-account-id", self.account.id)
        export(f"{account_name}-role-name", self.account.role_name)


def pulumi_program() -> None:
    """Execute creating the stack."""
    aws_account_id = get_aws_account_id()
    export("aws-account-id", aws_account_id)
    env = get_config("proj:env")
    export("env", env)

    # Create Resources Here

    organization_root_id = get_organization().roots[0].id

    central_infra_ou = organizations.OrganizationalUnit(
        "CentralizedInfrastructure",
        name="CentralizedInfrastructure",
        parent_id=organization_root_id,
        tags=common_tags_native(),
    )
    central_infra_prod_ou = organizations.OrganizationalUnit(
        "CentralInfraProd",
        name="Prod",
        parent_id=central_infra_ou.id,
        tags=common_tags_native(),
        opts=ResourceOptions(parent=central_infra_ou, delete_before_replace=True),
    )

    non_qualified_workload_ou = organizations.OrganizationalUnit(
        "NonQualifiedWorkloads",
        name="NonQualifiedWorkloads",
        parent_id=organization_root_id,
        tags=common_tags_native(),
    )
    _ = organizations.OrganizationalUnit(
        "NonQualifiedWorkloadProd",
        name="Prod",
        parent_id=non_qualified_workload_ou.id,
        tags=common_tags_native(),
        opts=ResourceOptions(parent=non_qualified_workload_ou, delete_before_replace=True),
    )
    workload_dev_ou = organizations.OrganizationalUnit(
        "NonQualifiedWorkloadDev",
        name="Dev",
        parent_id=non_qualified_workload_ou.id,
        tags=common_tags_native(),
        opts=ResourceOptions(parent=non_qualified_workload_ou, delete_before_replace=True),
    )
    central_infra_account_name = "central-infra-prod"
    central_infra_account = AwsAccount(ou=central_infra_prod_ou, account_name=central_infra_account_name)
    central_infra_role_arn = central_infra_account.account.id.apply(
        lambda x: f"arn:aws:iam::{x}:role/{DEFAULT_ORG_ACCESS_ROLE_NAME}"
    )
    assume_role = ProviderAssumeRoleArgs(role_arn=central_infra_role_arn, session_name="pulumi")
    central_infra_provider = Provider(
        f"{central_infra_account_name}",
        assume_role=assume_role,
        allowed_account_ids=[central_infra_account.account.id],
        region="us-east-1",
        opts=ResourceOptions(
            parent=central_infra_account,
        ),
    )
    central_state_bucket = s3.Bucket(
        "central-infra-state",
        tags=common_tags_native(),
        opts=ResourceOptions(
            provider=central_infra_provider,
            parent=central_infra_account,
        ),
    )
    _ = ssm.Parameter(
        "central-infra-state-bucket-name",
        type=ssm.ParameterType.STRING,
        name="/org-managed/infra-state-bucket-name",
        tags=common_tags(),
        value=central_state_bucket.bucket_name.apply(lambda x: f"{x}"),
        opts=ResourceOptions(provider=central_infra_provider, parent=central_infra_account),
    )
    kms_key_arn = get_config("proj:kms_key_id")
    assert isinstance(kms_key_arn, str), (
        f"Expected proj:kms_key_id to be a string, got {kms_key_arn} of type {type(kms_key_arn)}"
    )
    _ = ssm.Parameter(
        "central-infra-shared-kms-key-arn",
        type=ssm.ParameterType.STRING,
        name="/org-managed/infra-state-kms-key-arn",
        tags=common_tags(),
        value=kms_key_arn,
        opts=ResourceOptions(provider=central_infra_provider, parent=central_infra_account),
    )

    # TODO: create github OIDC for the central infra repo
    central_infra_prod_github_oidc = iam.OidcProvider(
        "central-infra-repo-github-oidc-provider",
        url="https://token.actions.githubusercontent.com",
        client_id_list=["sts.amazonaws.com"],
        thumbprint_list=["6938fd4d98bab03faadb97b34396831e3780aea1"],  # GitHub's root CA thumbprint
        tags=common_tags_native(),
        opts=ResourceOptions(provider=central_infra_provider, parent=central_infra_account),
    )
    preview_assume_role_policy_doc = central_infra_prod_github_oidc.arn.apply(
        lambda oidc_provider_arn: get_policy_document(
            statements=[
                GetPolicyDocumentStatementArgs(
                    effect="Allow",
                    principals=[
                        GetPolicyDocumentStatementPrincipalArgs(type="Federated", identifiers=[oidc_provider_arn])
                    ],
                    actions=["sts:AssumeRoleWithWebIdentity"],
                    conditions=[
                        GetPolicyDocumentStatementConditionArgs(
                            test="StringLike",
                            variable="token.actions.githubusercontent.com:sub",
                            values=[
                                f"repo:{CENTRAL_INFRA_GITHUB_ORG_NAME}/{CENTRAL_INFRA_REPO_NAME}:*"  # ref:refs/heads/initial-steps"
                            ],
                        ),
                        GetPolicyDocumentStatementConditionArgs(
                            test="StringEquals",
                            variable="token.actions.githubusercontent.com:aud",
                            values=["sts.amazonaws.com"],
                        ),
                    ],
                )
            ]
        )
    )
    deploy_assume_role_policy_doc = central_infra_prod_github_oidc.arn.apply(
        lambda oidc_provider_arn: get_policy_document(
            statements=[
                GetPolicyDocumentStatementArgs(
                    effect="Allow",
                    principals=[
                        GetPolicyDocumentStatementPrincipalArgs(type="Federated", identifiers=[oidc_provider_arn])
                    ],
                    actions=["sts:AssumeRoleWithWebIdentity"],
                    conditions=[
                        GetPolicyDocumentStatementConditionArgs(
                            test="StringEquals",
                            variable="token.actions.githubusercontent.com:sub",
                            values=[
                                f"repo:{CENTRAL_INFRA_GITHUB_ORG_NAME}/{CENTRAL_INFRA_REPO_NAME}:ref:refs/heads/main"
                            ],
                        ),
                        GetPolicyDocumentStatementConditionArgs(
                            test="StringEquals",
                            variable="token.actions.githubusercontent.com:aud",
                            values=["sts.amazonaws.com"],
                        ),
                    ],
                )
            ]
        )
    )

    central_infra_deploy_role = iam.Role(
        "central-infra-repo-deploy",
        role_name=f"InfraDeploy--{CENTRAL_INFRA_REPO_NAME}",
        assume_role_policy_document=deploy_assume_role_policy_doc.json,
        managed_policy_arns=["arn:aws:iam::aws:policy/AdministratorAccess"],
        tags=common_tags_native(),
        opts=ResourceOptions(provider=central_infra_provider, parent=central_infra_account),
    )
    deploy_in_workload_account_assume_role_policy = central_infra_deploy_role.arn.apply(
        lambda arn: get_policy_document(
            statements=[
                GetPolicyDocumentStatementArgs(
                    effect="Allow",
                    actions=["sts:AssumeRole"],
                    principals=[
                        GetPolicyDocumentStatementPrincipalArgs(
                            type="AWS",
                            identifiers=[arn],
                        )
                    ],
                )
            ]
        )
    )
    # TODO: lock this back down
    deploy_in_workload_account_assume_role_policy = central_infra_account.account.id.apply(
        lambda id: get_policy_document(
            statements=[
                GetPolicyDocumentStatementArgs(
                    effect="Allow",
                    actions=["sts:AssumeRole"],
                    principals=[
                        GetPolicyDocumentStatementPrincipalArgs(
                            type="AWS",
                            identifiers=[f"arn:aws:iam::{id}:root"],
                        )
                    ],
                )
            ]
        )
    )

    central_infra_preview_role = iam.Role(
        "central-infra-repo-preview",
        role_name=f"InfraPreview--{CENTRAL_INFRA_REPO_NAME}",
        assume_role_policy_document=preview_assume_role_policy_doc.json,
        managed_policy_arns=["arn:aws:iam::aws:policy/ReadOnlyAccess"],
        policies=[
            iam.RolePolicyArgs(
                policy_name="InfraKmsDecrypt",
                policy_document=get_policy_document(
                    statements=[
                        GetPolicyDocumentStatementArgs(
                            effect="Allow",
                            actions=[
                                "kms:Decrypt",
                                "kms:Encrypt",  # unclear why Encrypt is required to run a Preview...but Pulumi gives an error if it's not included
                            ],
                            resources=[kms_key_arn],
                        )
                    ]
                ).json,
            )
        ],
        tags=common_tags_native(),
        opts=ResourceOptions(provider=central_infra_provider, parent=central_infra_account),
    )
    preview_in_workload_account_assume_role_policy = central_infra_preview_role.arn.apply(
        lambda arn: get_policy_document(
            statements=[
                GetPolicyDocumentStatementArgs(
                    effect="Allow",
                    actions=["sts:AssumeRole"],
                    principals=[
                        GetPolicyDocumentStatementPrincipalArgs(
                            type="AWS",
                            identifiers=[arn],
                        )
                    ],
                )
            ]
        )
    )
    # TODO: lock this back down to just the central_infra_preview_role
    preview_in_workload_account_assume_role_policy = central_infra_account.account.id.apply(
        lambda id: get_policy_document(
            statements=[
                GetPolicyDocumentStatementArgs(
                    effect="Allow",
                    actions=["sts:AssumeRole"],
                    principals=[
                        GetPolicyDocumentStatementPrincipalArgs(
                            type="AWS",
                            identifiers=[f"arn:aws:iam::{id}:root"],
                        )
                    ],
                )
            ]
        )
    )

    biotasker_dev_account = AwsAccount(ou=workload_dev_ou, account_name="biotasker-dev")

    dev_account_data = [biotasker_dev_account.account_info_kwargs]
    all_dev_accounts_resolved = Output.all(
        *[
            Output.all(account["id"], account["name"]).apply(lambda vals: {"id": vals[0], "name": vals[1]})
            for account in dev_account_data
        ]
    )

    def build_workload(resolved_accounts: list[dict[str, str]]) -> str:
        # Convert resolved dicts to Pydantic AwsAccountInfo models
        dev_accounts = [AwsAccountInfo(**acc) for acc in resolved_accounts]

        logical_workload = AwsLogicalWorkload(
            name="biotasker",
            dev_accounts=dev_accounts,  # Insert all resolved dev accounts
        )

        return logical_workload.model_dump_json()

    workload_name = "biotasker"
    _ = ssm.Parameter(
        f"{workload_name}-workload-info-for-central-infra",
        type=ssm.ParameterType.STRING,
        name=f"{WORKLOAD_INFO_SSM_PARAM_PREFIX}/{workload_name}",
        description="Hold the logical workload information so that Central Infra account can deploy various resources within them.",
        tags=common_tags(),
        value=all_dev_accounts_resolved.apply(build_workload),
        opts=ResourceOptions(provider=central_infra_provider, parent=central_infra_account),
    )
    biotasker_role_arn = biotasker_dev_account.account.id.apply(
        lambda x: f"arn:aws:iam::{x}:role/{DEFAULT_ORG_ACCESS_ROLE_NAME}"
    )
    assume_role = ProviderAssumeRoleArgs(role_arn=biotasker_role_arn, session_name="blah")
    biotasker_provider = Provider(
        "biotasker-dev",
        assume_role=assume_role,
        region="us-east-1",
        opts=ResourceOptions(
            parent=biotasker_dev_account,
        ),
    )
    _ = iam.Role(
        f"central-infra-repo-deploy-in-{workload_name}",
        role_name=f"InfraDeploy--{CENTRAL_INFRA_REPO_NAME}",
        assume_role_policy_document=deploy_in_workload_account_assume_role_policy.json,
        managed_policy_arns=["arn:aws:iam::aws:policy/AdministratorAccess"],
        tags=common_tags_native(),
        opts=ResourceOptions(provider=biotasker_provider, parent=biotasker_dev_account),
    )
    _ = iam.Role(
        f"central-infra-repo-preview-in-{workload_name}",
        role_name=f"InfraPreview--{CENTRAL_INFRA_REPO_NAME}",
        assume_role_policy_document=preview_in_workload_account_assume_role_policy.json,
        managed_policy_arns=["arn:aws:iam::aws:policy/ReadOnlyAccess"],
        policies=[
            iam.RolePolicyArgs(
                policy_name="InfraKmsDecrypt",
                policy_document=get_policy_document(
                    statements=[
                        GetPolicyDocumentStatementArgs(
                            effect="Allow",
                            actions=[
                                "kms:Decrypt",
                                "kms:Encrypt",  # unclear why Encrypt is required to run a Preview...but Pulumi gives an error if it's not included
                            ],
                            resources=[kms_key_arn],
                        )
                    ]
                ).json,
            )
        ],
        tags=common_tags_native(),
        opts=ResourceOptions(provider=biotasker_provider, parent=biotasker_dev_account),
    )

    # TODO: move these SSM Parameters to the central infra stack

    # TODO: delegate SSO management https://docs.aws.amazon.com/singlesignon/latest/userguide/delegated-admin-how-to-register.html
    admin_permission_set = AwsSsoPermissionSet(
        "LowRiskAccountAdminAccess", "Low Risk Account Admin Access", ["AdministratorAccess"]
    )
    _ = AwsSsoPermissionSetAccountAssignments(
        account_id=central_infra_account.account.id,
        account_name=central_infra_account_name,
        permission_set=admin_permission_set,
        users=["eli.fine"],
    )
    _ = AwsSsoPermissionSetAccountAssignments(
        account_id=biotasker_dev_account.account.id,
        account_name="biotasker-dev",
        permission_set=admin_permission_set,
        users=["eli.fine"],
    )

    _ = Command(  # I think this needs to be after at least 1 other account is created, but maybe not
        "enable-aws-service-access",
        create="aws organizations enable-aws-service-access --service-principal account.amazonaws.com",
    )
