# Discord Community Manager

Bot Discord communautaire avec administration automatisée, statistiques agrégées, rapports périodiques (PDF/CSV), détection de pics d'activité et dashboard Web responsive.

## Installation rapide

1. Créez une application sur le [Discord Developer Portal](https://discord.com/developers/applications) :
   - Activez l'intent **Server Members Intent** dans l'onglet *Bot*.
   - Ajoutez l'URL de redirection OAuth2 (`http://localhost:8000/auth/callback`) dans l'onglet *OAuth2*.
2. Copiez `.env.example` vers `.env` et renseignez vos identifiants.
3. Démarrez les conteneurs :
   ```bash
   docker compose up -d --build
   ```
4. Sur votre serveur Discord, exécutez `/setup` puis `/setup-check`.
