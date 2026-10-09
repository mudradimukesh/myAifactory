import hypothesis


def test_deep_profile_registered_with_2000_examples_not_derandomized() -> None:
    profile = hypothesis.settings.get_profile("deep")
    assert (profile.derandomize, profile.max_examples) == (False, 2000)
