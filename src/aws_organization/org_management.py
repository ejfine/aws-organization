from lab_auto_pulumi import UserInfo


def get_org_admins() -> list[UserInfo]:
    org_admins: list[UserInfo] = []
    org_admins.append(UserInfo(username="eli.fine"))
    return org_admins
