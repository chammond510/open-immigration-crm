# DeepL

Translates client messages and intake answers when staff ask, with the firm's DeepL API Pro key. It is in Beta.

## What it does

- **Where staff use it.** A **Translate** button under each communication on contact and matter pages, and on each intake submission, translates the text into the language staff choose.
- **The original stays.** The translation is kept beside the original, never in its place.
- **Labelled.** It is shown as a machine translation by DeepL, with the language it came from, and a reminder to check it against the original before relying on it.
- **One per language.** A newer translation into the same language replaces the older one.
- **Languages.** It translates into English (US), Spanish, Portuguese (Brazil), French, Chinese (simplified), Arabic, Russian, Ukrainian, Korean, Japanese, Vietnamese, Turkish, and Indonesian. DeepL does not translate into Haitian Creole; with [AI translation](../../docs/AI.md) turned on, the firm's AI connection can.

## Why it needs a Pro key

DeepL's terms for its API Free plan forbid sending personal data through it, and clients' messages are personal data. The connection refuses a Free key, which ends in `:fx`, and sends nothing with it.

Under the Pro terms, DeepL deletes texts once they are translated and does not use them to improve its services. DeepL offers a data processing agreement for Pro accounts. Put one in place before translating client messages, and check where DeepL processes the firm's data.

## Setting it up

1. **Create a key.** In the firm's DeepL Pro account, create an API key. A key limited to translating text and reading usage is enough.
2. **Connect.** In the CRM, open **Connections → DeepL** and paste the key. Review what the connection sends, and connect. The connection test reads the account's usage.
3. **Turn off preview mode.** New connections start in preview mode, which records each translation request without sending it. Turn it off on the connection's page when the firm is ready.

## What is sent

Only the text of the message or answers staff choose to translate, the language to translate into, and a fixed note that the texts come from clients of an immigration law firm, which guides DeepL's choice of words. DeepL answers with the translations and the language each text was written in.

Each request goes through the outbound policy. The policy's record of the request names the language only, never the text.
