-- Remove the retired dashboard permission code and preserve access to Appearance settings.

INSERT INTO NL2SQL_APP_ROLE_PERMISSIONS (ROLE_ID, PERMISSION_CODE)
SELECT stale.ROLE_ID, 'menu.settings_appearance'
  FROM NL2SQL_APP_ROLE_PERMISSIONS stale
 WHERE stale.PERMISSION_CODE = 'dashboard.view'
   AND NOT EXISTS (
       SELECT 1
         FROM NL2SQL_APP_ROLE_PERMISSIONS existing
        WHERE existing.ROLE_ID = stale.ROLE_ID
          AND existing.PERMISSION_CODE = 'menu.settings_appearance'
   );

DELETE FROM NL2SQL_APP_ROLE_PERMISSIONS
 WHERE PERMISSION_CODE = 'dashboard.view';

INSERT INTO NL2SQL_SCHEMA_MIGRATIONS (VERSION_NO, DESCRIPTION, CHECKSUM)
VALUES (9, 'RBAC permission cleanup for appearance settings', 'runtime-verified');
