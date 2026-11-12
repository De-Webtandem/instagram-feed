# instagram-feed

Houdt de Instagram-strook op dewebtandem.nl actueel zonder Netlify-deploys.

**Publieke repo, sync elk uur** (besluit 12 nov 2026): Actions-minuten zijn voor publieke repo's gratis en tellen niet
mee voor de 2000 minuten van de organisatie. Er staan geen geheimen in de code; tokens staan alleen in GitHub Secrets.
Workflows draaien niet op pull requests, dus forks krijgen nooit toegang tot de secrets.

```
GitHub Actions (elk uur)  ->  Instagram API  ->  sync.py (ffmpeg)  ->  Cloudflare Pages (feed.json + m/*)
                                                                          ^
dewebtandem.nl  script.js haalt feed.json op; lukt dat niet, dan blijven de vaste tegels staan
```

- **sync.py**: één bestand, alleen standaardbibliotheek + ffmpeg. Elke deploy bevat de 8 actieve posts plus de
  bestanden van de vorige generatie (zodat een gecachete feed.json nooit naar een 404 wijst).
  Ongewijzigd = geen deploy. Instagram of Cloudflare even weg = niets publiceren (exit 75).
  Token geweigerd = direct alarm (exit 2).
- **Video**: geluid eruit, max 30 s, 540 px, max 4 MB. Lukt dat niet, of zit er muziek met licentie in
  (geen `media_url`), dan wordt het een stilstaand beeld. Noodschakelaar: repo-variabele `INSTAGRAM_VIDEO=0`.
- **Monitoring**: Healthchecks.io krijgt een ping na elke geslaagde run en alarmeert als er 24 uur geen kwam;
  een harde fout pingt `/fail`. GitHub mailt ook bij elke mislukte run.
- **Onderhoud** (maandag): token verlengen, Actions-verbruik van de organisatie controleren (waarschuwing > 1500 min),
  en in een publieke repo een keepalive-commit (GitHub zet geplande workflows anders na 60 dagen uit).

## Lokaal testen

```bash
FFMPEG=/pad/naar/ffmpeg python -m unittest -v test_sync
python dev_serve.py <map-met-feed> 8090
```

Website lokaal: `http://localhost:8080/index.html?instafeed=http://localhost:8090/feed.json`
(de `?instafeed=`-override werkt alleen op localhost).

## Eenmalige inrichting (handmatig)

1. Instagram @dewebtandem is een Business- of Creator-account.
2. Meta for Developers: app van het type **Business**, product "Instagram API with Instagram Login",
   @dewebtandem toevoegen, long-lived token (60 dagen) genereren.
3. Cloudflare (gratis, zonder betaalmethode): Pages-project via **Direct Upload**, API-token met alleen
   *Account > Cloudflare Pages > Edit*.
4. Healthchecks.io (gratis): check met periode 1 uur en grace 23 uur, e-mailmelding aan.
5. GitHub, deze repo:
   - Secrets: `IG_TOKEN`, `CLOUDFLARE_API_TOKEN`, `HEALTHCHECK_URL`, `GH_ONDERHOUD_PAT`
     (fine-grained, alleen deze repo: *Secrets read/write*; organisatie: *Administration read* voor het verbruik; 1 jaar geldig).
   - Variabelen: `PAGES_URL`, `PAGES_PROJECT`, `CLOUDFLARE_ACCOUNT_ID`.
6. Eerste run handmatig (`workflow_dispatch`, force), daarna `data-insta-feed` in index.html van de website invullen.

## Terugkerend

- Elk jaar: `GH_ONDERHOUD_PAT` vernieuwen (anders faalt de tokenverlenging en verloopt het Instagram-token ~60 dagen later).
- Bij alarm "token geweigerd": nieuw token genereren in Meta en `IG_TOKEN` vervangen.
