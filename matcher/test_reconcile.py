from matcher.reconcile import normalize_address


def test_normalize_address_expands_northwest():
    assert normalize_address("1250 NW Franklin Street") == (
        normalize_address("1250 Northwest Franklin St")
    )