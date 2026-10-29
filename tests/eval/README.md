# Evals

The evals check the use case end to end: does a KYC request take the right path and get the right decision?

## What is here

- `flows.json` lists the flows: the request, the reply to each pause, the distinct steps the run must take (in order of first appearance), the KYC recommendation and whether an account is opened.
  The requests use real records of the mock registry at `https://608b7a57737e470017b74d29.mockapi.io/api/v1/customers`, except `reject_unknown_id` and `reject_no_details`, which test an unknown ID and an empty request.
  [Try the flows](../../docs/how-to/try-the-flows.md) walks through the same flows by hand.
- `test_flows.py` runs each flow the way `adk web` does: ADK's `Runner`, the real model, the registry, the World Bank agent and the accounts API.

## Run them

From the repository root, with `GOOGLE_API_KEY` in `retail_kyc_onboarding/.env` (and `OPENAI_API_KEY` to include the last fallback model):

```bash
FLOW_LIVE_EVALS=1 pytest tests/eval -m live
```

They are skipped in the default `pytest tests` run, because they need a key, call real services and open real accounts in the mock API.
The KYC decision comes from a model, so a rare different answer is possible; the routing after it is deterministic.

## Add a flow

Add an entry to `flows.json` with the steps by their ids in `workflow/graph.py`, then run the evals.
