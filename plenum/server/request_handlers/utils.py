from _sha256 import sha256

from common.serializers.serialization import domain_state_serializer
from plenum.common.constants import STEWARD, ROLE


def is_steward(state, nym, is_committed: bool = False):
    role = get_role(state, nym, is_committed)
    return role == STEWARD


def get_role(state, nym, is_committed: bool = False):
    nymData = get_nym_details(state, nym, is_committed)
    if not nymData:
        return {}
    else:
        return nymData.get(ROLE)


def get_nym_details(state, nym, is_committed: bool = False):
    key = nym_to_state_key(nym)
    data = state.get(key, is_committed)
    if not data:
        return {}
    return domain_state_serializer.deserialize(data)


def nym_to_state_key(nym: str) -> bytes:
    return sha256(nym.encode()).digest()
