-- 明示的 migration。能力 API は package/table を作成しない。
-- DATA USER に artifact table 権限を与えず、同じ transaction に監査を参加させる。
CREATE OR REPLACE PACKAGE NL2SQL_ONT_ACTION_TX AUTHID DEFINER AS
  PROCEDURE LOCK_SCOPE(p_profile VARCHAR2, p_actor VARCHAR2,
    p_head_etag VARCHAR2, p_binding_id VARCHAR2, p_binding_etag VARCHAR2,
    p_execution_id VARCHAR2, p_key_id VARCHAR2, p_existing OUT CLOB, p_key OUT CLOB);
  PROCEDURE SAVE_RECORD(p_id VARCHAR2, p_hash VARCHAR2, p_etag VARCHAR2,
    p_kind VARCHAR2, p_payload CLOB);
END NL2SQL_ONT_ACTION_TX;
/
CREATE OR REPLACE PACKAGE BODY NL2SQL_ONT_ACTION_TX AS
  g_profile VARCHAR2(128);
  g_actor VARCHAR2(128);
  g_execution VARCHAR2(128);
  g_key VARCHAR2(128);
  PROCEDURE LOCK_SCOPE(p_profile VARCHAR2, p_actor VARCHAR2,
    p_head_etag VARCHAR2, p_binding_id VARCHAR2, p_binding_etag VARCHAR2,
    p_execution_id VARCHAR2, p_key_id VARCHAR2, p_existing OUT CLOB, p_key OUT CLOB) IS
    v_etag VARCHAR2(256);
    v_session VARCHAR2(128);
    v_kind VARCHAR2(48);
  BEGIN
    g_profile := NULL;
    -- 管理接続以外は認証済み DATA USER の client identifier と一致させる。
    IF SYS_CONTEXT('USERENV','SESSION_USER') <> SYS_CONTEXT('USERENV','CURRENT_USER')
       AND NVL(SYS_CONTEXT('USERENV','CLIENT_IDENTIFIER'), '#') <> p_actor THEN
      RAISE_APPLICATION_ERROR(-20040, 'application actor mismatch');
    END IF;
    SELECT ETAG INTO v_etag FROM NL2SQL_ONTOLOGY_ARTIFACTS
      WHERE SESSION_ID = 'profile-ontology:' || p_profile
        AND ARTIFACT_TYPE = 'ontology_published_head' FOR UPDATE WAIT 5;
    IF v_etag <> p_head_etag THEN
      RAISE_APPLICATION_ERROR(-20041, 'published scope changed');
    END IF;
    SELECT ETAG, SESSION_ID, ARTIFACT_TYPE INTO v_etag, v_session, v_kind
      FROM NL2SQL_ONTOLOGY_ARTIFACTS WHERE ARTIFACT_ID = p_binding_id FOR UPDATE WAIT 5;
    IF v_etag <> p_binding_etag OR v_session <> 'profile-ontology:' || p_profile
       OR v_kind <> 'ontology_capability_binding' THEN
      RAISE_APPLICATION_ERROR(-20041, 'capability binding changed');
    END IF;
    p_existing := NULL;
    p_key := NULL;
    BEGIN
      SELECT PAYLOAD_JSON INTO p_existing FROM NL2SQL_ONTOLOGY_ARTIFACTS
       WHERE ARTIFACT_ID = p_execution_id AND SESSION_ID = 'profile-ontology:' || p_profile
         AND ARTIFACT_TYPE = 'ontology_action_execution';
    EXCEPTION WHEN NO_DATA_FOUND THEN NULL;
    END;
    BEGIN
      SELECT PAYLOAD_JSON INTO p_key FROM NL2SQL_ONTOLOGY_ARTIFACTS
       WHERE ARTIFACT_ID = p_key_id AND SESSION_ID = 'profile-ontology:' || p_profile
         AND ARTIFACT_TYPE = 'ontology_action_key';
    EXCEPTION WHEN NO_DATA_FOUND THEN NULL;
    END;
    g_profile := p_profile; g_actor := p_actor; g_execution := p_execution_id; g_key := p_key_id;
  END LOCK_SCOPE;
  PROCEDURE SAVE_RECORD(p_id VARCHAR2, p_hash VARCHAR2, p_etag VARCHAR2,
    p_kind VARCHAR2, p_payload CLOB) IS
  BEGIN
    IF g_profile IS NULL OR NOT (
      (p_id = g_execution AND p_kind = 'ontology_action_execution') OR
      (p_id = g_key AND p_kind = 'ontology_action_key')) THEN
      RAISE_APPLICATION_ERROR(-20042, 'action transaction scope required');
    END IF;
    IF JSON_VALUE(p_payload, '$.artifact_id') <> p_id OR
       JSON_VALUE(p_payload, '$.session_id') <> 'profile-ontology:' || g_profile OR
       JSON_VALUE(p_payload, '$.profile_id') <> g_profile OR
       JSON_VALUE(p_payload, '$.artifact_type') <> p_kind THEN
      RAISE_APPLICATION_ERROR(-20042, 'audit scope mismatch');
    END IF;
    INSERT INTO NL2SQL_ONTOLOGY_ARTIFACTS
      (ARTIFACT_ID, SESSION_ID, ARTIFACT_TYPE, CONTENT_HASH, VERSION_NO, ETAG, PAYLOAD_JSON)
      VALUES(p_id, 'profile-ontology:' || g_profile, p_kind, p_hash, 1, p_etag, p_payload);
    -- COMMIT / autonomous transaction を使用しない。呼出側の DML と同時に確定。
  END SAVE_RECORD;
END NL2SQL_ONT_ACTION_TX;
/
DECLARE
  v_errors NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_errors FROM USER_ERRORS WHERE NAME = 'NL2SQL_ONT_ACTION_TX';
  IF v_errors > 0 THEN RAISE_APPLICATION_ERROR(-20043, 'action transaction package compilation failed'); END IF;
  BEGIN
    EXECUTE IMMEDIATE 'GRANT EXECUTE ON NL2SQL_ONT_ACTION_TX TO NL2SQL_APP_DB_ROLE';
  EXCEPTION WHEN OTHERS THEN IF SQLCODE <> -1917 THEN RAISE; END IF;
  END;
END;
/
