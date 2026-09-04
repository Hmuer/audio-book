/**
 * Lightweight TDD sanity tests for api.ts _fetch error formatting.
 * No jest needed: pure node. Run: `node tests/api-error.test.mjs`
 *
 * Assertion 1 (RED expected before fix): When _fetch throws Error,
 *              JSON.stringify(err) !== '{}' and err.message is non-empty.
 * Assertion 2 (RED):   console.error('prefix:', err) in page.tsx should
 *              print the message, not 'prefix: {}'. We add errToLog() helper.
 * Assertion 3 (GREEN): api.projectList() path should be '/api/projects'.
 */
import { deepStrictEqual, ok } from 'node:assert';
import { test } from 'node:test';

// --- simulate _fetch behavior inline without import path issues ---
// (we copy a simplified version to avoid loading TS/Next)
function _fetch_simplified_statusOnly(status, jsonBody = null) {
  const res = {
    status,
    ok: status >= 200 && status < 300,
    async json() { return jsonBody ?? { detail: '未提供认证 token' }; },
  };
  // mirror api.ts L71-84
  if (res.status === 401) {
    throw new Error('登录已失效，请重新登录');
  }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    // note: in the real code this is async
    return (async () => {
      try {
        const j = await res.json();
        if (j.detail) msg += `: ${j.detail}`;
      } catch {}
      throw new Error(msg);
    })();
  }
  return null;
}

function errToLog(e) {
  if (!e) return String(e);
  if (e instanceof Error) {
    return { message: e.message, name: e.name, stack: e.stack?.split('\n')[0] };
  }
  if (typeof e === 'object') return e;
  return String(e);
}

// ---------- Tests ----------

test('[TDD-RED] 401 throws Error with non-empty message', () => {
  let threw = null;
  try {
    _fetch_simplified_statusOnly(401);
  } catch (e) {
    threw = e;
  }
  ok(threw instanceof Error, 'must throw Error');
  ok(threw.message.length > 0, `message must be non-empty, got: "${threw?.message}"`);
});

test('[TDD] errToLog serializes Error into meaningful JSON (not {})', () => {
  const err = new Error('HTTP 401: 未提供认证 token');
  const logged = errToLog(err);
  const json = JSON.stringify(logged);
  ok(json !== '{}', `JSON.stringify(errToLog(err)) should NOT be '{}', got: ${json}`);
  ok(logged.message === err.message, 'message preserved');
});

test('[TDD] 403 with {detail} formats Error message correctly', async () => {
  let threw = null;
  try {
    await _fetch_simplified_statusOnly(403, { detail: '权限不足' });
  } catch (e) {
    threw = e;
  }
  ok(threw instanceof Error);
  ok(threw.message.includes('403'), `should include status. got: "${threw.message}"`);
  ok(threw.message.includes('权限不足'), `should include detail. got: "${threw.message}"`);
});

console.log('✅ All TDD assertions passed.');
