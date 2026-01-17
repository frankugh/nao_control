from cmdrec.guard import is_guarded


def test_guard_defaults_to_all_guarded_when_missing():
    policy = {"threshold": 0.5, "margin": 0.1}
    assert is_guarded("DANCE", policy) is True
    assert is_guarded("BOX", policy) is True


def test_guard_default_true_with_unguarded_labels():
    policy = {
        "guard": {
            "default": True,
            "unguarded_labels": ["BOX", "STOP"],
        }
    }
    assert is_guarded("BOX", policy) is False
    assert is_guarded("STOP", policy) is False
    assert is_guarded("DANCE", policy) is True


def test_guard_default_false_with_guarded_labels():
    policy = {
        "guard": {
            "default": False,
            "guarded_labels": ["DANCE", "WALK_WITH_ME"],
        }
    }
    assert is_guarded("DANCE", policy) is True
    assert is_guarded("WALK_WITH_ME", policy) is True
    assert is_guarded("BOX", policy) is False


def test_guard_overrides_take_precedence():
    policy = {
        "guard": {
            "default": True,
            "unguarded_labels": ["BOX"],
        }
    }
    assert is_guarded("BOX", policy, guarded_labels_override=["BOX"]) is True
    assert is_guarded("DANCE", policy, unguarded_labels_override=["DANCE"]) is False
