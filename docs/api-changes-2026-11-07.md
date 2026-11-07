# WarEra API changes — scan of 2026-11-07

Result of calling every endpoint stored in `openapi.json` with live data and
comparing the response with the stored example (about 100 of the 107 were
called). It also covers the strategic-resources / military-rank update that
came with the same game patch.

## How to read the comparison

- Most stored examples are **trimmed**, so "field exists live but not in the
  example" mostly means the example is short. It is not a change.
- "Field in the example but missing live" is the useful signal. It can also come
  from sampling a different entity (a different event type, battle, MU, ...).
- No path that exists in both the example and the live response changed type.

## New endpoints (added to `openapi.json`)

| Endpoint | Returns |
|---|---|
| `alliance.getStrategicResourceBonuses` | Resource copies of every non-suspended member and the bonuses they give |
| `country.getStrategicResourceBonuses` | Bonuses that apply to a country (its whole alliance if it has one) |
| `government.getCongressSeats` | Congress seats of a country |

95 more query endpoints exist in the game's frontend but the API refuses them
for API tokens, so they are not in the spec.

## Endpoints that are gone (404)

- `workOffer.getById`, `workOffer.getWageStats`, `workOffer.getWorkOfferByCompanyId`,
  `workOffer.getWorkOffersPaginated`
- `worker.getWorkers`, `worker.getTotalWorkersCount`
- `work.getStatsByWorker`, `work.getStatsByWorkerAndCompany`

`work.getStatsByUserId`, `work.getStatsByCompany`, `work.getStatsByEmployee` and
`work.getLastByEmployee` still exist but answer "Premium required" for our key.

## Data that was removed or changed

| Where | Change |
|---|---|
| `party.getById`, `party.getManyPaginated` | The whole `ethics` block is gone. New: `congressList`, `favoriteGovType`, `primaryWinner`, `councilMembers`, `members` |
| `company.getRecommendedRegionIdsByItemCode` | Always returns `[]` now (deposits are gone) |
| `country.getAllCountries`, `country.getCountryById` | `taxes.selfWork` removed. New: `allianceId`, `income.development`, `dates.lastAllianceLeftAt` |
| `managerOffer.getManyPaginated` | `citizenship` replaced by `minManagement` |
| `user.getUserLite`, `user.getUserById` | `dates.lastHiresAt`, `dates.lastWorkOfferApplications` gone |
| `contribution.getCountryUnrestContributions`, `contribution.getRegionContributions` | `user` is a plain id instead of `{_id, username}`; `country` added |
| `shop.getTopGiftGivers` | `username` removed |
| `article.*` | `stats.gemTips` removed |
| `gameConfig.getGameConfig` | Skills are `skills.<name>.levels.<n>`; many new sections (alliance, mission, mercenaryContract, upgradesConfig, ...) |
| `user.getUserById` | `skills.<name>.level` no longer includes prestige. The prestige levels are in `skills.<name>.prestige`; `leveling.prestige` is the **unspent** prestige points. Skills also carry a `strategic` bonus |
| `skills.attack.militaryRankPercent` | Already doubled by the game (rank 99 = 61%) |

New fields worth knowing: `government.getByCountryId.customTitles`,
`gameConfig.getDates.monthlyMissionRegenAt`, `inventory.fetchCurrentEquipment.ammo`,
bounty / money-pool fields on battles.

## Strategic resources (new mechanic)

- 1 / 4 / 9 copies of a resource are level 1 / 2 / 3.
- Per level: rare earths +3 Production, gold +1 Management, lithium +10 Energy,
  coal +5 Precision, diamonds +10 Health, **uranium +2 Loot chance** (the
  changelog said development income, the API says loot chance).
- Everyone in an alliance gets the bonus of all members' copies together;
  suspended members neither share nor add. Without an alliance only the
  country's own regions count.
- A player's own bonus is `skills.<name>.strategic` on `user.getUserById`.

## Not checked (resolved afterwards)

- `alliance.getByIds`, `battleLootSummary.getByBattleAndUser`,
  `gameStat.getEquipmentAvgByCode`: examples added to the spec and the explorer.
- `poll.getPoll`, `poll.getPollVoters`: poll ids are not listed anywhere, they sit in
  article HTML as `poll-id="..."`. Examples added (live shape: question, options with
  votes, endsAt, myVote/canVote; voters as `{items: [...]}`).
- `upgradeConstruction.listConstructions`: needs `regionId` or `countryId` (400
  otherwise; now in the description). No construction is running anywhere, so the example
  is `{items: []}`.
- `itemOffer.getById`: answers 404 for every id we could find and no reachable endpoint
  lists offers, so there is no sample. Noted in its description.
- `employee.getMyEmployees`, `employee.getMyWorkforceSummary`, `employeeEvent.getMine`,
  `employeeEvent.getPendingCount`: read the calling account's own data; our keys have no
  employees, so they return empty. Existing examples kept and the description says so.

## Follow-up work

See the "What was done" section at the bottom of this file.

## What was done

**Specs and explorer**

- `openapi.json`: refreshed 60 stored examples from live responses (short
  samples, long strings and lists trimmed) and added examples for
  `alliance.getByIds`, `battleLootSummary.getByBattleAndUser` and
  `gameStat.getEquipmentAvgByCode`. The eight removed endpoints are marked
  `deprecated: true` with a "REMOVED" description; the four premium-only `work.*`
  endpoints say so in their description.
- API explorer (`rijksoverheid_web`): removed endpoints get a red "removed" badge and
  are dimmed. The premium-routing list now holds the four `work.*` procedures that still
  exist. 38 of the explorer's own stored example outputs (`database/api_examples.json`)
  were refreshed from the live responses.

**Dead code in the Discord bot and data fetcher**

- Removed `cogs/tasks/ethics_watcher.py` (watched `party.ethics`, which no longer exists).
- Removed `cogs/tasks/production.py` (hourly deposit/ethics leaderboard poll from
  `company.getRecommendedRegionIdsByItemCode`, which now returns nothing) and the
  `/peil productie` option that triggered it.
- Removed `cogs/commands/bonus.py` (`/bonus`, `/topbonus`, `/verhuiskosten`): they only
  read the leaderboard that task wrote.
- `cogs/commands/bedrijfswinst.py`, `cogs/commands/pillenfabrieken.py`: no more
  `worker.getWorkers` calls (there are no workers to list), and the
  deposit / strategic-resource / ethics bonus lookups are replaced by
  `company.getProductionBonus`'s `total`.
- `cogs/commands/ecobuild.py`: production bonus now also comes from
  `company.getProductionBonus`.
- `services/full_fetcher.py`: the base-upgrade cooldown no longer looks up each country's
  ruling party's militarism ethic (gone); it is the 8 hours from
  `gameConfig.upgrade.regionUpgradeCooldownHours` everywhere, which is what the old code
  already returned for every country.

**Left on purpose**

- `services/db/production.py` and its tables (`specialization_top`, `deposit_top`,
  `country_item_ethic`) are kept as history; nothing writes to them any more.
- `/bedrijfswinst` and `/pillenfabrieken` are still built on the old job-market
  model (wages, worker fidelity). They no longer call dead endpoints, but they
  show 0 workers; they need a redesign around hired employees, or removal.
