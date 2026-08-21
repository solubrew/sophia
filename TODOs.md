

# New Features
- each connection should be a service url + apikey (if needed) such that one service url can be used multiple times with different api keys
- each of these connection accounts should have a name, user account email, and usage schedule like a rotateing 75million tokens reset at 1am, 6am, 11am, 4pm, 8pm or other schedule and/or weekly, monthly limits
- the system should break up the time available from each account schedule and use accordingly to make sure all capabilities are available
- structured_call end point
- ensure interface compatibility with hermes - use either the custom API or Custom endpoint setup and add details to the documentation on setup for hermes
   (○) Azure Foundry (OpenAI-style or Anthropic-style endpoint, your Azure AI deployment)
   (○) custom (direct API)
   (○) DeepInfra — 100+ open models, pay-per-use
   (○) Upstage (Solar API)
   (○) Custom endpoint (enter URL manually)
   (○) Configure auxiliary models...
   (○) Leave unchanged
