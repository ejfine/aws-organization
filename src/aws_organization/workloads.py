from .legacy_biotasker import create_legacy_biotasker
from .lib import CommonWorkloadKwargs
from .lib import OrganizationalUnits


def create_workloads(*, org_units: OrganizationalUnits, common_workload_kwargs: CommonWorkloadKwargs):
    create_legacy_biotasker(org_units, common_workload_kwargs)
