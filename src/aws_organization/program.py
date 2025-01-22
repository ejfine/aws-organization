import logging

from pulumi import ComponentResource
from pulumi import ResourceOptions
from pulumi import export
from pulumi_aws_native import organizations
from pulumi_command.local import Command

from .config import common_tags_native
from .pulumi_ephemeral_deploy.utils import get_aws_account_id
from .pulumi_ephemeral_deploy.utils import get_config

logger = logging.getLogger(__name__)


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
            opts=ResourceOptions(parent=ou),
            account_name=account_name,
            email=f"ejfine+{account_name}@gmail.com",
            parent_ids=[ou.id],
            tags=common_tags_native(),
        )
        export(f"{account_name}-account-id", self.account.id)
        export(f"{account_name}-role-name", self.account.role_name)


def pulumi_program() -> None:
    """Execute creating the stack."""
    aws_account_id = get_aws_account_id()
    export("aws-account-id", aws_account_id)
    env = get_config("proj:env")
    export("env", env)

    # Create Resources Here

    organization_root_id = get_config("proj:org_root_id")
    assert isinstance(organization_root_id, str), (
        f"Expected proj:org_root_id to be a string, got {organization_root_id} of type {type(organization_root_id)}"
    )
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
    _ = organizations.OrganizationalUnit(
        "NonQualifiedWorkloadDev",
        name="Dev",
        parent_id=non_qualified_workload_ou.id,
        tags=common_tags_native(),
        opts=ResourceOptions(parent=non_qualified_workload_ou, delete_before_replace=True),
    )

    _ = AwsAccount(ou=central_infra_prod_ou, account_name="centralized-infra-prod")
    _ = Command(
        "enable-aws-service-access",
        create="aws organizations enable-aws-service-access --service-principal account.amazonaws.com",
    )

    # TODO: delegate SSO management https://docs.aws.amazon.com/singlesignon/latest/userguide/delegated-admin-how-to-register.html
