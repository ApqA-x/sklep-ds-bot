# Discord Command UX Research

Дата: 2026-04-12

Цель файла: дать будущим агентам быстрый старт по тому, как делать Discord slash commands в этом репо более правильными, красивыми и user-friendly. Это не финальный дизайн и не код, а исследовательская база: ссылки, выводы, ограничения текущей архитектуры и идеи для следующих итераций.

## Как было собрано

- Запускались 5 read-only субагентов: по аргументам Discord, UX ответов, open-source примерам, локальной архитектуре и общим CLI-принципам.
- 4 агента завершили исследование; агент по open-source примерам завис и был остановлен.
- Open-source секция дополнена вручную проверенными источниками: официальные примеры `discord.py`, RoboDanny paginator и ReactionMenu.
- Большой код из open-source проектов не копировать. Использовать источники как reference/inspiration и соблюдать лицензии.

## Контекст этого репо

Текущие Discord-команды:

- `voice_tracker/commands.py`: `/settings`, `/track`, `/inspect`, основная логика voice tracker команд, permission checks, route parsing, форматирование ответов.
- `voice_tracker/shuffle.py`: `/shuffle`, async-логика перемещения людей между voice/stage channels, exclude parsing, permission checks, result formatting.
- `voice_tracker/appcommands.py`: общий каталог команд и сериализация dataclass-команд в Discord HTTP payload.
- `services/commands.py`: Discord runtime handler для `/settings`, `/track`, `/inspect`; отвечает embed-ом ephemeral.
- `services/shuffle.py`: Discord runtime handler для `/shuffle`; делает `defer(ephemeral=True)` и отправляет followup.

Важные ограничения:

- Деплой сейчас single-guild: `DISCORD_GUILD_ID` обязателен, команды регистрируются guild-scoped.
- `services.commands` и `services.shuffle` оба вызывают bulk overwrite общего каталога команд. Если сервисы разных версий, один может перезаписать каталог устаревшим payload.
- `MessageEmbed` в `voice_tracker/discord_models.py` сейчас умеет только `title`, `description`, `color`.
- `InteractionResponseData` сейчас не моделирует `allowed_mentions`, `components`, `view`, `fields`, `footer`.
- `voice_tracker/appcommands.py` уже умеет пропускать `default_member_permissions` при dataclass conversion, но voice-команды его пока не задают.
- Для Discord constraints нельзя полагаться только на UI: runtime validation всё равно нужна, потому что autocomplete и client-side filters не являются security boundary.

## Главные правила для будущих агентов

- Держать домены команд понятными: `/settings`, `/track`, `/inspect`, `/shuffle`.
- Ветки поведения делать subcommands/subcommand groups, а не строковым `mode`, если у веток разные обязательные аргументы.
- Для аргументов использовать native Discord option types: `CHANNEL`, `USER`, `ROLE`, `INTEGER`, `STRING`, а не free-form string, где можно избежать.
- Для маленьких фиксированных списков использовать `choices`; для динамических списков использовать `autocomplete`; не смешивать `choices` и `autocomplete` на одном option.
- Для числовых диапазонов предпочитать `min_value` / `max_value`, если не нужен именно dropdown с choices.
- Для channel options всегда ставить `channel_types`, но оставлять backend validation resolved channel guild/type.
- Админские/config ответы делать ephemeral по умолчанию.
- Долгие команды подтверждать быстро: `defer(ephemeral=True, thinking=True)`, потом edit/followup.
- Длинные списки не пихать в один description: использовать embed fields или pagination buttons.
- Не разрешать ping-и из user input по умолчанию: использовать conservative `allowed_mentions`.
- Ошибки писать конкретно и recoverable: что не так, какое право/тип нужен, что выбрать дальше.

## Источники и что из них взять

| Источник | Что взять |
| --- | --- |
| [Discord Application Commands](https://docs.discord.com/developers/interactions/application-commands) | Command object, option structure, required-before-optional, max 25 options, choices, channel_types, autocomplete, localization, guild/global registration. |
| [Discord Receiving and Responding](https://docs.discord.com/developers/interactions/receiving-and-responding) | 3-second initial response deadline, defer/followup, ephemeral flag, autocomplete response type and response shape. |
| [Discord Message Resource](https://docs.discord.com/developers/resources/message) | Embed object, allowed_mentions, message payloads, attachment notes. |
| [Discord Embed Limits](https://docs.discord.com/developers/resources/message#embed-limits) | 256 title chars, 4096 description chars, 25 fields, 1024 chars per field value, 6000 chars total across embeds. |
| [Discord Allowed Mentions](https://docs.discord.com/developers/resources/message#allowed-mentions-object) | Suppress or explicitly allow mentions; avoid role/everyone pings from user-provided content. |
| [Discord Component Reference](https://docs.discord.com/developers/components/reference) | Buttons, selects, custom_id rules, button styles, button text guidance, select option limits. |
| [Discord Message Formatting](https://docs.discord.com/developers/reference#message-formatting) | Timestamp format like `<t:unix:R>`, mentions, markdown behavior. |
| [Discord Channel Types](https://docs.discord.com/developers/resources/channel#channel-object-channel-types) | Canonical IDs: text `0`, voice `2`, stage voice `13`. |
| [Discord Permissions](https://docs.discord.com/developers/topics/permissions#bitwise-permission-flags) | Permission bitfield source: Administrator, Manage Guild, Connect, Move Members, etc. |
| [discord.py Interactions API](https://discordpy.readthedocs.io/en/stable/interactions/api.html) | `InteractionResponse.send_message`, `defer`, `followup`, `discord.ui.View`, app_commands decorators and objects. |
| [discord.py persistent view example](https://github.com/Rapptz/discord.py/blob/master/examples/views/persistent.py) | Persistent views, unique `custom_id`, per-user interaction checks, dynamic custom_id patterns. |
| [RoboDanny paginator](https://github.com/Rapptz/RoboDanny/blob/rewrite/cogs/utils/paginator.py) | Production-grade pagination patterns from the `discord.py` author; use as design reference, not copy-paste target. |
| [ReactionMenu](https://github.com/Defxult/reactionmenu) | MIT library for discord.py pagination with buttons/selects; useful for UX ideas and possible dependency discussion. |
| [Command Line Interface Guidelines](https://clig.dev/) | General command UX: clear names, helpful output, validation, progress, recoverability. |
| [Heroku CLI Style Guide](https://devcenter.heroku.com/articles/cli-style-guide) | Topic/verb grammar, flags vs args, stable output ideas. |
| [Shopify CLI Command Guidelines](https://shopify.github.io/cli/cli-kit/command-guidelines.html) | Command/argument/flag clarity; optimize names for clarity over brevity. |
| [Salesforce CLI Commands and Topics](https://developer.salesforce.com/docs/platform/salesforce-cli-plugin/guide/topics.html) | Grammar model: topic, action, resource/sub-action. |
| [Salesforce CLI Flags and Arguments](https://developer.salesforce.com/docs/platform/salesforce-cli-plugin/guide/flags.html) | Options should modify one task, not secretly change the command's fundamental behavior. |
| [GOV.UK Error Message](https://design-system.service.gov.uk/components/error-message/) | Error messages should be concise, specific, and tell the user how to fix the problem. |
| [Atlassian Error Messages](https://atlassian.design/foundations/content/designing-messages/error-messages) | Scannable errors, next steps, reveal deeper details only when needed. |

## Аргументы команд: как лучше делать

Текущее хорошее:

- `/settings mode mode:<all|none|specific>` уже использует choices.
- `summary-set` ограничивает `channel` до text channels.
- `track` и `inspect` channel options ограничены voice/stage channels.
- `history all` и `history pick` сейчас ограничены choices от `1` до `10`.

Что улучшить позже:

- Для `history limit` и `history pick` можно заменить 10 choices на integer `min_value=1`, `max_value=10`, если dropdown не нужен.
- Добавить поддержку `min_value`, `max_value`, `min_length`, `max_length`, `autocomplete`, localization fields в локальные command dataclasses и serializer.
- Добавить command payload validation tests:
  - command/option name length and regex;
  - description length;
  - max 25 options/choices;
  - unique option names inside each array;
  - required options before optional;
  - no `choices` together with `autocomplete`;
  - valid subcommand nesting depth;
  - choice value type matches option type.
- Для `/shuffle exclude` free-form string заменить или дополнить:
  - несколькими typed `USER` options вроде `exclude1`, `exclude2`, `exclude3`;
  - string autocomplete по участникам/голосу, если нужен более гибкий ввод;
  - role-based exclusions, если появится продуктовая потребность.

## Permissions

Будущая идея: добавить top-level `default_member_permissions`, чтобы Discord UI скрывал команды от большинства пользователей по умолчанию.

Предлагаемая карта:

- `/settings`: Manage Guild.
- `/track`: Manage Guild.
- `/inspect`: Administrator, если хотим сохранить текущую строгость; либо отдельный продуктовый пересмотр для read-only inspectors.
- `/shuffle`: Move Members.

Важно:

- `default_member_permissions` не заменяет runtime checks.
- Нужно оставить `BOT_ADMIN_USER_IDS`, потому что это repo-specific allowlist.
- Discord server admins могут менять command permissions, поэтому backend всё равно должен проверять права.
- Discord API представляет permission bitsets как строки; при расширении serializer проверить, нужно ли приводить int к string перед HTTP registration.

## Ответы команд: как красиво выводить

Паттерн по умолчанию:

- Для настроек и админских результатов: ephemeral.
- Для ошибок прав доступа: ephemeral.
- Для потенциально шумных inspect/search ответов: ephemeral.
- Для результата, который явно надо показать каналу: отдельная кнопка/команда "share" или явная public-команда.

Формат embed:

- Title: короткий домен, например `Voice Tracker` или `Shuffle`.
- Description: результат в 1-4 строки, не вся таблица.
- Fields: totals, channel, duration, participants, skipped/failures.
- Footer: page/status, например `Page 1/3` или `Requested by ...`.
- Timestamps: использовать Discord format `<t:unix:f>` и `<t:unix:R>`.

Пагинация:

- Начинать думать о pagination, когда список больше 10-15 строк или есть риск приблизиться к лимитам embed.
- Кнопки: `Prev`, `Next`, `Refresh`, `Close`.
- Ограничивать callbacks через `interaction_check` только invoking user/admin.
- На timeout отключать кнопки или удалять controls.
- Для category/page jump можно использовать select menu, но помнить про max 25 options.

Allowed mentions:

- По умолчанию suppress all или максимально консервативно.
- Каналы `<#id>` безопасны как navigation hints.
- User mentions использовать только когда реально нужен ping.
- Никогда не давать user input пинговать `@everyone`, роли или произвольных пользователей без явного allowlist.

## Ошибки и copywriting

Плохие примеры:

- `Insufficient permissions.`
- `unsupported channel type`
- `Command failed. Check service logs.`

Лучше:

- `You need Manage Server to change voice tracking settings.`
- `Choose a voice or stage channel for this command.`
- `I could not move members because I need Move Members in <#channel>.`
- `That channel belongs to another guild. Pick a channel from this server.`

Для русскоязычного сервера можно позже добавить локализации, но default command/option names лучше оставить стабильными на английском, потому что Discord interaction payload всё равно использует default names.

## Future feature ideas

Быстрые улучшения:

- `/settings summary-test`: отправить preview recap в configured summary channel.
- `/settings fallback-show` и `/settings fallback-clear`: явно управлять fallback summary channel.
- Улучшить `/track add/remove/clear` ответы, особенно когда mode=`all`, а stored tracked channels не влияют до mode=`specific`.
- `/track import-category`: добавить voice/stage channels из категории.
- `/track import-all`: добавить текущие voice/stage channels сервера в tracked list.

Inspect/reporting:

- `/inspect active mine`: показать активную сессию пользователя или канал, где он сейчас сидит.
- `/inspect active user user:<member>`: админский поиск по пользователю.
- `/inspect history page`: пагинация closed sessions.
- Stable session IDs в `/inspect history`, чтобы не полагаться только на recency index `1..10`.
- `repost recap` / `send summary here` для закрытой сессии.

Shuffle:

- `/shuffle preview`: рассчитать план перемещений без фактического move.
- Confirm-before-move через buttons: `Confirm`, `Cancel`, danger style для destructive action.
- Shuffle presets: сохранить наборы каналов и потом запускать их одним subcommand.
- Better exclusions: typed users, roles, maybe "exclude bots" toggle.
- Result summary с counts: moved, skipped, excluded, failures, inaccessible channels.

Components/selects:

- `ChannelSelect` для summary/tracked channels, если будем переходить на discord.py View API.
- `UserSelect` для inspect user/report commands.
- `RoleSelect` для admin role config.
- `StringSelect` для time ranges: today, 7 days, 30 days.

Architecture:

- Централизовать command registration, чтобы один сервис владел bulk overwrite.
- Расширить `MessageEmbed` / response models для fields/footer/allowed_mentions/components.
- Добавить response builder, который умеет превращать domain result в Discord-friendly embed/pages.
- Добавить integration-ish tests для `services/commands.py` и `services/shuffle.py`, а не только core logic.

## Implementation checklist для будущего агента

Перед изменением команд:

- Проверить current command payload через `appcommands.default_commands()`.
- Убедиться, что изменения отражены в `tests/test_appcommands.py`.
- Не ломать текущие route names без миграционного решения.
- Если добавляется localization, не менять default names, по которым route parser определяет команды.
- Если добавляются components, проверить, как discord.py service path и dataclass test path будут представлены одновременно.
- Если добавляется `default_member_permissions`, оставить runtime checks.
- Если добавляется autocomplete, помнить: autocomplete suggestions не являются enforced choices.

Перед изменением ответов:

- Проверить embed limits.
- Проверить отсутствие нежелательных pings.
- Для long-running commands использовать defer/followup.
- Для state-changing commands подумать об idempotency или confirm flow.
- Для pagination добавить timeout и interaction ownership.
