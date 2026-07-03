from have_agent.ids import new_ulid

CROCKFORD = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_shape():
    u = new_ulid()
    assert len(u) == 26
    assert set(u) <= CROCKFORD


def test_unique_and_sorted():
    ids = [new_ulid() for _ in range(5000)]
    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids), "ULIDs must sort in creation order"
