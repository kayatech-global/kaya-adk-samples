# Provenance of this directory

This is a private copy of the flow engine (`retail_kyc_onboarding._engine`), version 0.1.0, vendored into the agent so it runs with nothing from the platform that exported it.
It uses relative imports only, so it does not need to be installed and does not clash with another engine in the same process.
Do not edit it: to change behaviour for one deployment, change `runtime.yaml`, `build_providers(...)` or the `MODELS`, `PROMPTS` and `TOOLS` maps in `retail_kyc_onboarding/settings.py`; to change the engine, export again.
