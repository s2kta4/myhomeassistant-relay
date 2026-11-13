# My Home Assistant Relay

![My Home Assistant Relay: your home, wherever you are. Dashboard shown with sample data.](https://myhomeassistant.top/assets/social-relay.png)

**Your home, wherever you are.** Bring selected Home Assistant readings and everyday controls together in your private Relay dashboard.

- **Easy setup:** choose entities using Home Assistant’s picker.
- **No port forwarding for Relay:** your Home Assistant connects using outbound HTTPS.
- **Try Relay free:** start with the free plan and explore paid options for larger dashboards.
- **Your choice of controls:** you decide what to share; sensitive controls need explicit approval.

[Explore My Home Assistant Relay →](https://myhomeassistant.top/relay) · [View the setup guide →](https://myhomeassistant.top/relay-guide)

## Requirements

- Home Assistant 2026.9.0 or newer and internet access.
- An account and Relay connection at myhomeassistant.top. Plan limits and current options are shown in your account.
- HACS, if installing with HACS.

## Installation

Until this repository is accepted into the default HACS catalog, add it as a custom repository:

1. In Home Assistant, open **HACS** and select the three-dot menu, then **Custom repositories**.
2. Add `https://github.com/s2kta4/myhomeassistant-relay` with the **Integration** category.
3. Find **My Home Assistant Relay** in HACS and download it, then restart Home Assistant.
4. Open **Settings → Devices & services → Add integration**, and select **My Home Assistant Relay**.

These instructions apply after the public repository and release are published. For manual installation and the full setup guide, see the [Relay guide](https://myhomeassistant.top/relay-guide).

## Set up the entity picker

1. In your myhomeassistant.top account, open your Relay setup and create or copy its private connection key.
2. In the integration setup, select **Choose entities in this integration**, then paste the key.
3. Select the entities you want to share within your plan's limit and finish setup.
4. Open your Relay dashboard to see selected states and the supported controls. You can later change the selection in the integration's options.

Keep the connection key private. Use only one Relay client for each connection: don't run the automatic integration and a Relay YAML package or UI automation for the same connection at the same time. The separate **Use a UI automation** option installs a helper action for the generated UI automation; it is not needed for the automatic entity picker.

Locks and covers remain read-only unless their controls are explicitly enabled in your service account. Scripts can perform broader actions in your home; select and enable controls with care. The Relay dashboard exposes selected entities rather than the full Home Assistant interface.

To revoke a connection key or manage your Relay connection, use your myhomeassistant.top account. See the [Relay guide](https://myhomeassistant.top/relay-guide) for support and current product information.

## License

MIT. This independent integration is not affiliated with Home Assistant or Nabu Casa.
