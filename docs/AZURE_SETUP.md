# Azure setup (Microsoft Foundry + Voice Live)

You do these steps in the Azure portal. Never paste keys into a chat.

## 1. Create the Foundry resource
1. Open https://portal.azure.com → **Create a resource** → search **Microsoft Foundry** → **Create**.
2. Fill in:
   - Subscription: yours.
   - Resource group: **Create new** → `voicebot-rg`.
   - Region: **Sweden Central** (it has `gpt-realtime-mini` and `gpt-realtime` in Voice Live).
   - Name: a unique name, for example `voicebot-<something>`. It becomes part of the endpoint.
   - Default project: you can accept it; the bot does not use it.
3. **Review + create** → **Create**. Wait for "Deployment succeeded".

No model deployment is needed. Voice Live provides `gpt-realtime-mini` itself (a "predeployed" model).

## 2. Get the endpoint and key
1. Open the resource → **Resource Management → Keys and Endpoint**.
2. You need **Endpoint** (looks like `https://<name>.services.ai.azure.com/`) and **KEY 1**.
3. Put both straight into the file on the Pi (step 4 in the README):
   `sudoedit /etc/voicebot/voicebot.env`

## 3. Budget alert
1. Portal → **Cost Management** → **Budgets** → **Add**.
2. Scope: the `voicebot-rg` resource group. Reset period: monthly.
3. Amount: start with a small number (for example 20 in your currency).
4. Alerts: 50 %, 80 %, 100 % of actual cost → your email.

A budget only sends emails. It does not stop spending.

## Notes
- Pricing: see the Voice Live pricing page. `gpt-realtime-mini` is in the cheaper "Standard" tier.
- A newer `gpt-realtime-2.1-mini` is also available in Sweden Central. Try it later by changing
  `VOICELIVE_MODEL` in the env file.
- `azure_semantic_vad_multilingual` lists English and some other languages, but not Ukrainian or Russian.
  If it cuts you off or waits too long in Ukrainian/Russian, set `TURN_DETECTION=server_vad`.
