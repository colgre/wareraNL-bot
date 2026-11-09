# WarEra API scan of 2026-11-09 (weekly league patch)

- **League:** the only league procedure in the frontend is `league.getMyLeague`. It answers
  403 "API tokens cannot access this endpoint", so it cannot be used or added.
  Mercenary contract reads (`mercenaryContract.getContracts/getById/getActiveForBattle`) are also blocked.
- **Scanner blind spot fixed:** `scan_warera_endpoints.py` only read the chunks from the build
  manifest and missed the shared chunks (`pages/_app`, `main`, ...). It now also reads the chunks
  referenced by the homepage HTML (267 query procedures found instead of 144).
- **18 reachable endpoints added to `openapi.json`:** `articleInteraction.getArticleLikers/Dislikers/Tippers/GemTippers`,
  `battle.getActiveBattlesByCountry`, `battle.getViewersCount`, `mu.getReputationBuyStatus`,
  `party.getInactiveMemberIds`, `manager.getByCompanyId`, `managerOffer.getByCompanyId`, `user.getUserSkills`,
  `gameStat.getBadgeStats/getMilestoneStats/getPpMoneyValueByItem`, `user.getTodayClaimCount`,
  `upgrade.getMapUpgrades`, `map.getMapData`, `mercenaryContractAuction.getActiveAuctions`
  (the frontend now uses this instead of `getPaginatedAuctions`, which still works).
- Reachable but deliberately not added: `klipy.search/trending` (GIF proxy), `skinStudio.getImageVersions`
  (cosmetics), `userFavoriteGif.list` (personal data of the key's account).
- The other ~145 query procedures are all blocked for API tokens.
