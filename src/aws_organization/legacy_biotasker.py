from ephemeral_pulumi_deploy.utils import common_tags
from ephemeral_pulumi_deploy.utils import common_tags_native
from pulumi import Output
from pulumi import ResourceOptions
from pulumi_aws_native import Provider
from pulumi_aws_native import ProviderAssumeRoleArgs
from pulumi_aws_native import iam
from pulumi_aws_native import ssm

from .constants import CENTRAL_INFRA_REPO_NAME
from .lib import DEFAULT_ORG_ACCESS_ROLE_NAME
from .lib import AwsAccount
from .lib import CommonWorkloadKwargs
from .lib import OrganizationalUnits
from .lib import create_pulumi_kms_role_policy_args
from .lib.shared_lib import WORKLOAD_INFO_SSM_PARAM_PREFIX
from .lib.shared_lib import AwsAccountInfo
from .lib.shared_lib import AwsLogicalWorkload


def create_legacy_biotasker(org_units: OrganizationalUnits, common_workload_kwargs: CommonWorkloadKwargs):
    biotasker_dev_account = AwsAccount(ou=org_units.non_qualified_workload_dev, account_name="biotasker-dev")

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
    _ = ssm.Parameter(  # TODO: consider DRY-ing this up with the parameter generation in lib.py
        f"{workload_name}-workload-info-for-central-infra",
        description="Hold the logical workload information so that Central Infra account can deploy various resources within them.",
        type=ssm.ParameterType.STRING,
        name=f"{WORKLOAD_INFO_SSM_PARAM_PREFIX}/{workload_name}",
        tags=common_tags(),
        value=all_dev_accounts_resolved.apply(build_workload),
        opts=ResourceOptions(
            provider=common_workload_kwargs["central_infra_provider"],
            parent=common_workload_kwargs["central_infra_account"],
        ),
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
        assume_role_policy_document=common_workload_kwargs["deploy_in_workload_account_assume_role_policy"].json,
        managed_policy_arns=["arn:aws:iam::aws:policy/AdministratorAccess"],
        tags=common_tags_native(),
        opts=ResourceOptions(provider=biotasker_provider, parent=biotasker_dev_account),
    )
    _ = iam.Role(
        f"central-infra-repo-preview-in-{workload_name}",
        role_name=f"InfraPreview--{CENTRAL_INFRA_REPO_NAME}",
        assume_role_policy_document=common_workload_kwargs["preview_in_workload_account_assume_role_policy"].json,
        policies=[create_pulumi_kms_role_policy_args(common_workload_kwargs["kms_key_arn"])],
        managed_policy_arns=["arn:aws:iam::aws:policy/ReadOnlyAccess"],
        tags=common_tags_native(),
        opts=ResourceOptions(provider=biotasker_provider, parent=biotasker_dev_account),
    )
