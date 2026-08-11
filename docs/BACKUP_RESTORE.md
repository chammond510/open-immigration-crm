# Backup and restore

A usable backup contains the PostgreSQL database **and** the private media volume from a coordinated recovery point. Protect backup material as strictly as the live system.

## Compose evaluation backup

From the repository directory:

```bash
mkdir -p backups
chmod 700 backups
docker compose exec -T db pg_dump -U opencrm -d opencrm -Fc > backups/database.dump
docker run --rm -v open-immigration-crm_media_data:/source:ro \
  -v "$PWD/backups:/backup" alpine:3.22 \
  tar -C /source -czf /backup/media.tar.gz .
chmod 600 backups/database.dump backups/media.tar.gz
```

Compose volume names can vary by directory/project name; confirm them with `docker volume ls`. Move backups to encrypted offsite storage, then remove local working copies under your retention policy.

## Restore drill

Restore into an isolated, access-restricted environment—not over the live system:

1. Create an empty PostgreSQL database and empty media volume.
2. Restore database with `pg_restore --clean --if-exists --no-owner` using the correct application role.
3. Extract the matching media archive into the media volume with restrictive ownership/permissions.
4. Start the matching application version.
5. Verify record counts, representative contacts/matters, a document download, staff login, and the audit trail.
6. Record recovery time and any manual corrections, then securely destroy the drill environment.

Test restores on a schedule. A backup job that reports success is not proof of recoverability.

## Export is not backup

The CSV and portable JSON exports are for portability and review. They omit authentication/session data and the JSON export does not copy uploaded bytes. They do not replace database and media backups.
