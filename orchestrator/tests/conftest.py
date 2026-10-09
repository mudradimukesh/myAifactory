import hypothesis

hypothesis.settings.register_profile("deep", max_examples=2000, derandomize=False)
