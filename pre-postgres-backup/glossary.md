# Glossary

Terms fed to the ASR decoder as a vocabulary bias, and to the minutes compiler as
spelling guidance.

**Order is priority order.** Whisper's `initial_prompt` is capped at ~224 tokens,
so this file is truncated from the bottom. Put the terms whose misspelling would
do the most damage at the top.

Why this matters more than it looks: a mangled product or person name fragments
the knowledge graph. If "Project Atlas" is transcribed three different ways, you
get three disconnected nodes for one thing, and no query finds all of it. Fixing
a name here is far cheaper than fixing it across a year of minutes.

Format: one term per line as a bullet. Anything after a ` - ` or `: ` is treated
as a comment for humans and is not sent to the model.

## People

Recurring attendees. Use the spelling you want to appear everywhere.

- Faraz
- Ali

## Products, features, projects

<!-- Add your feature names, epics, and internal project codenames here. -->

## Customers and partners

<!-- Add customer and partner names here. -->

## Acronyms and jargon

<!-- Add internal acronyms here, e.g. PRD, ICP, MRR, NPS. -->

- PRD
- ICP
