# ============== WARNING ==============================================================================
# File is managed by copier template: gh:LabAutomationAndScreening/copier-aws-organization.git
# See .config/.copier-managed-files.json for details.
#
# You are welcome to make changes to this file in your repo if they are custom to your project,
# but if the change should be shared with other projects, please backport it to the template repo.
# =====================================================================================================
from lab_auto_pulumi import UserInfo
from pydantic import BaseModel


class OrgAdmin(BaseModel):
    user_info: UserInfo
    enable_break_glass_access: bool = False


def get_org_admins() -> list[OrgAdmin]:
    """Define Admins.

    Example:
    ```
    org_admins: list[OrgAdmin] = [
        OrgAdmin(user_info=UserInfo(username="eli.fine@elifine.com"), enable_break_glass_access=False),
        OrgAdmin(user_info=UserInfo(username="mal.reynolds@firefly.star")),
    ]
    ```
    """
    org_admins: list[OrgAdmin] = []
    org_admins.append(OrgAdmin(user_info=UserInfo(username="eli.fine")))
    return org_admins
