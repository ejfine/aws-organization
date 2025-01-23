import logging

from pulumi import ComponentResource
from pulumi import Output
from pulumi import ResourceOptions
from pulumi import export
from pulumi_aws import identitystore as identitystore_classic
from pulumi_aws import ssoadmin
from pulumi_aws_native import organizations
from pulumi_command.local import Command

from .config import common_tags_native
from .pulumi_ephemeral_deploy.utils import get_aws_account_id
from .pulumi_ephemeral_deploy.utils import get_config

logger = logging.getLogger(__name__)


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
            filter=identitystore_classic.GetUserFilterArgs(attribute_path="UserName", attribute_value=name),
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
            role_name="OrganizationAccountAccessRole",
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

    central_infra_account = AwsAccount(ou=central_infra_prod_ou, account_name="central-infra-prod")
    _ = Command(
        "enable-aws-service-access",
        create="aws organizations enable-aws-service-access --service-principal account.amazonaws.com",
    )

    # TODO: delegate SSO management https://docs.aws.amazon.com/singlesignon/latest/userguide/delegated-admin-how-to-register.html
    admin_permission_set = AwsSsoPermissionSet(
        "LowRiskAccountAdminAccess", "Low Risk Account Admin Access", ["AdministratorAccess"]
    )
    _ = AwsSsoPermissionSetAccountAssignments(
        account_id=central_infra_account.account.id,
        account_name="central-infra-prod",
        permission_set=admin_permission_set,
        users=["eli.fine"],
    )
