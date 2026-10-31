# Alerte SP98 – stations TotalEnergies autour de Coulaines

Toutes les 15 minutes, un workflow GitHub Actions interroge le flux officiel des prix des
carburants, repère les stations TotalEnergies dans le rayon choisi et t'envoie une
notification push (ntfy) quand :

- le SP98 **redevient disponible** (ou est disponible pour la première fois) ;
- le **prix du SP98 change** dans une station où il est disponible ;
- le SP98 **passe en rupture** (notification discrète, priorité basse).

Au tout premier lancement, tu reçois un récapitulatif des stations surveillées : c'est ton test.

## Installation (≈ 10 minutes)

1. **ntfy sur ton téléphone** : installe l'appli *ntfy* (iOS / Android), puis « S'abonner à un topic »
   avec un nom long et aléatoire
2. **Dépôt GitHub** : crée un dépôt et envoie-y le contenu de ce dossier (dont `total_cache.json` et les deux fichiers
   de `.github/workflows/`).
   Un dépôt **public** est conseillé (minutes Actions illimitées). Ton adresse et ton topic restent
   dans les *secrets*, donc invisibles. En dépôt privé, 15 min ≈ 2 900 minutes/mois pour 2 000 offertes :
   passe alors le cron à `*/30`.
3. **Secrets** : Settings → Secrets and variables → Actions → *New repository secret* :
   - `ADDRESS` 
   - `NTFY_TOPIC` 
4. **Variable (optionnel)** : onglet *Variables* → `RADIUS_KM` (défaut 15).
5. **Premier lancement** : onglet Actions → « Alerte SP98 TotalEnergies » → *Run workflow*.
   Tu dois recevoir la notification « Surveillance SP98 active ».

## Recevoir aussi les alertes par mail (optionnel)

ntfy.sh refuse les mails envoyés sans compte : le mail part donc directement depuis GitHub, par SMTP.

1. Crée un **mot de passe d'application** chez ton fournisseur de mail (Gmail : validation en 2 étapes activée,
   puis https://myaccount.google.com/apppasswords). N'utilise jamais ton mot de passe principal.
2. Secrets du dépôt : `ALERT_EMAIL` (destinataire), `SMTP_USER` (adresse qui envoie, complète) et `SMTP_PASSWORD`
   (le mot de passe d'application).
3. Autre fournisseur que Gmail : variable `SMTP_HOST` (ex. `smtp.orange.fr`) ; port 587 par défaut.
4. Test : Actions → « Alerte SP98 TotalEnergies » → *Run workflow* → coche **test_mail**. Un échec est signalé par
   un avertissement jaune dans le résumé du run (`Mail non envoyé …`).

## Limites

- Une rupture non déclarée par le gérant apparaît « disponible » : l'alerte est un bon indicateur, pas une garantie.
- Le flux est actualisé toutes les ~10 min ; les crons GitHub peuvent avoir quelques minutes de retard.
- GitHub désactive les workflows planifiés après 60 jours sans activité sur le dépôt ; les commits
  de `state.json` (à chaque changement de prix) comptent comme activité.
- Réglages avancés (variables d'environnement) : `PRICE_DELTA`, `BRAND_REGEX`, `NTFY_SERVER`, `NTFY_TOKEN` — voir l'en-tête de `sp98_alert.py`.
