# Credential expiry regression

For a goal involving credentials, reproduce expiry through the real local provider and API. Set fixture expiry beyond the configured verifier clock-skew allowance; one second in the past was accepted under a 30-second allowance. Require the actual unauthorized response. This is an optional project-specific check, not an objective of this kit. The original application test is intentionally replaced by adapter lifecycle tests.
