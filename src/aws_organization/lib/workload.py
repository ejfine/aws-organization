import logging
from typing import TypedDict

from ephemeral_pulumi_deploy.utils import common_tags
from ephemeral_pulumi_deploy.utils import common_tags_native
from lab_auto_pulumi import WORKLOAD_INFO_SSM_PARAM_PREFIX
from lab_auto_pulumi import AwsAccountInfo
from lab_auto_pulumi import AwsLogicalWorkload
from pulumi import ComponentResource
from pulumi import Output
from pulumi import ResourceOptions
from pulumi_aws.iam import GetPolicyDocumentResult
from pulumi_aws.iam import GetPolicyDocumentStatementArgs
from pulumi_aws.iam import get_policy_document
from pulumi_aws_native import Provider
from pulumi_aws_native import ProviderAssumeRoleArgs
from pulumi_aws_native import iam
from pulumi_aws_native import organizations
from pulumi_aws_native import ssm

from .account import AwsAccount
from .constants import CENTRAL_INFRA_REPO_NAME

logger = logging.getLogger(__name__)

DEFAULT_ORG_ACCESS_ROLE_NAME = "OrganizationAccountAccessRole"


def create_pulumi_kms_role_policy_args(kms_key_arn: str) -> iam.RolePolicyArgs:
    return iam.RolePolicyArgs(
        policy_document=get_policy_document(
            statements=[
                GetPolicyDocumentStatementArgs(
                    actions=[
                        "kms:Decrypt",
                        "kms:Encrypt",  # unclear why Encrypt is required to run a Preview...but Pulumi gives an error if it's not included
                    ],
                    effect="Allow",
                    resources=[kms_key_arn],
                )
            ]
        ).json,
        policy_name="InfraKmsDecrypt",
    )


class AwsWorkload(ComponentResource):
    def __init__(  # noqa: PLR0913 # yes, this is a lot of arguments, but they're all kwargs
        self,
        *,
        workload_name: str,
        prod_ou: organizations.OrganizationalUnit | None = None,
        staging_ou: organizations.OrganizationalUnit | None = None,
        dev_ou: organizations.OrganizationalUnit | None = None,
        prod_account_name_suffixes: list[str] | None = None,
        staging_account_name_suffixes: list[str] | None = None,
        dev_account_name_suffixes: list[str] | None = None,
        central_infra_account: AwsAccount,
        deploy_in_workload_account_assume_role_policy: Output[GetPolicyDocumentResult],
        preview_in_workload_account_assume_role_policy: Output[GetPolicyDocumentResult],
        kms_key_arn: str,
        central_infra_provider: Provider,
    ):
        super().__init__("labauto:aws-organization:AwsWorkload", workload_name, None)
        self.preview_in_workload_account_assume_role_policy = preview_in_workload_account_assume_role_policy
        self.deploy_in_workload_account_assume_role_policy = deploy_in_workload_account_assume_role_policy
        self.kms_key_arn = kms_key_arn

        self.prod_accounts: list[AwsAccount] = []
        self.staging_accounts: list[AwsAccount] = []
        self.dev_accounts: list[AwsAccount] = []

        for suffixes, ou, account_list in [
            (prod_account_name_suffixes, prod_ou, self.prod_accounts),
            (staging_account_name_suffixes, staging_ou, self.staging_accounts),
            (dev_account_name_suffixes, dev_ou, self.dev_accounts),
        ]:
            if suffixes is None:
                continue
            assert len(suffixes) == 1, (
                f"Only a single account name suffix in each environment tier is well tested/supported currently, not {suffixes}"
            )
            assert ou is not None
            for account_name_suffix in suffixes:
                account_name = f"{workload_name}-{account_name_suffix}"
                account_resource = AwsAccount(
                    account_name=account_name,
                    ou=ou,
                    parent=self,
                )
                account_list.append(account_resource)
                self._create_central_infra_roles(account_resource, account_name)
        dev_account_data: list[Output[dict[str, str]]] = []
        staging_account_data: list[Output[dict[str, str]]] = []
        prod_account_data: list[Output[dict[str, str]]] = []
        for data_list, account_list in [
            (dev_account_data, self.dev_accounts),
            (staging_account_data, self.staging_accounts),
            (prod_account_data, self.prod_accounts),
        ]:
            data_list.extend([account.account_info_kwargs for account in account_list])

        all_prod_accounts_resolved = Output.all(
            *[
                Output.all(account["id"], account["name"]).apply(lambda vals: {"id": vals[0], "name": vals[1]})
                for account in prod_account_data
            ]
        )
        all_staging_accounts_resolved = Output.all(
            *[
                Output.all(account["id"], account["name"]).apply(lambda vals: {"id": vals[0], "name": vals[1]})
                for account in staging_account_data
            ]
        )
        all_dev_accounts_resolved = Output.all(
            *[
                Output.all(account["id"], account["name"]).apply(lambda vals: {"id": vals[0], "name": vals[1]})
                for account in dev_account_data
            ]
        )

        def build_workload(
            resolved_prod_accounts: list[dict[str, str]],
            resolved_staging_accounts: list[dict[str, str]],
            resolved_dev_accounts: list[dict[str, str]],
        ) -> str:
            # Convert resolved dicts to Pydantic AwsAccountInfo models
            prod_accounts_info = [AwsAccountInfo(**acc) for acc in resolved_prod_accounts]
            staging_accounts_info = [AwsAccountInfo(**acc) for acc in resolved_staging_accounts]
            dev_accounts_info = [AwsAccountInfo(**acc) for acc in resolved_dev_accounts]

            logical_workload = AwsLogicalWorkload(
                name=workload_name,
                prod_accounts=prod_accounts_info,
                staging_accounts=staging_accounts_info,
                dev_accounts=dev_accounts_info,
            )

            return logical_workload.model_dump_json()

        _ = ssm.Parameter(
            f"{workload_name}-workload-info-for-central-infra",
            type=ssm.ParameterType.STRING,
            name=f"{WORKLOAD_INFO_SSM_PARAM_PREFIX}/{workload_name}",
            description="Hold the logical workload information so that Central Infra account can deploy various resources within them.",
            tags=common_tags(),
            value=Output.all(
                all_prod_accounts_resolved, all_staging_accounts_resolved, all_dev_accounts_resolved
            ).apply(lambda args: build_workload(*args)),
            opts=ResourceOptions(
                provider=central_infra_provider,
                parent=central_infra_account,
                delete_before_replace=True,
                depends_on=[account.wait_after_account_create for account in self.all_accounts],
            ),
        )

    def _create_central_infra_roles(self, account_resource: AwsAccount, account_name: str):
        provider_role_arn = account_resource.account.id.apply(
            lambda x: f"arn:aws:iam::{x}:role/{DEFAULT_ORG_ACCESS_ROLE_NAME}"
        )
        assume_role = ProviderAssumeRoleArgs(role_arn=provider_role_arn, session_name="pulumi")
        account_provider = Provider(
            account_name,
            assume_role=assume_role,
            region="us-east-1",
            opts=ResourceOptions(parent=account_resource, depends_on=[account_resource.wait_after_account_create]),
        )
        _ = iam.Role(
            f"central-infra-repo-deploy-in-{account_name}",
            role_name=f"InfraDeploy--{CENTRAL_INFRA_REPO_NAME}",
            assume_role_policy_document=self.deploy_in_workload_account_assume_role_policy.json,
            managed_policy_arns=["arn:aws:iam::aws:policy/AdministratorAccess"],
            tags=common_tags_native(),
            opts=ResourceOptions(provider=account_provider, parent=account_resource),
        )
        _ = iam.Role(  # TODO: DRY this up with the one in program.py. and also add the necessary S3 permissions for preview
            f"central-infra-repo-preview-in-{account_name}",
            role_name=f"InfraPreview--{CENTRAL_INFRA_REPO_NAME}",
            assume_role_policy_document=self.preview_in_workload_account_assume_role_policy.json,
            managed_policy_arns=["arn:aws:iam::aws:policy/ReadOnlyAccess"],
            policies=[create_pulumi_kms_role_policy_args(self.kms_key_arn)],
            tags=common_tags_native(),
            opts=ResourceOptions(provider=account_provider, parent=account_resource),
        )

    @property
    def all_accounts(self) -> tuple[AwsAccount, ...]:
        return tuple(self.prod_accounts + self.staging_accounts + self.dev_accounts)


class CommonWorkloadKwargs(TypedDict):
    central_infra_account: AwsAccount
    deploy_in_workload_account_assume_role_policy: Output[GetPolicyDocumentResult]
    preview_in_workload_account_assume_role_policy: Output[GetPolicyDocumentResult]
    kms_key_arn: str
    central_infra_provider: Provider
