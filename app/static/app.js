/* 表单与请求：动作集中在 ACTIONS 里，失败必须写到页面上 */
const HANDOFF_CREDENTIALS = {
  // 领取到的凭据只放在本标签页的会话存储里：刷新还在，换标签页就重新领取。
  get(taskId, purpose) {
    try {
      return JSON.parse(sessionStorage.getItem(`hg:${handoffId()}:${taskId}:${purpose}`));
    } catch (error) {
      return null;
    }
  },
  set(taskId, purpose, credential) {
    try {
      sessionStorage.setItem(`hg:${handoffId()}:${taskId}:${purpose}`,
        JSON.stringify(credential));
    } catch (error) {
      /* 存储被禁用时只是刷新后要重新领取 */
    }
  },
};

function handoffId() {
  const node = document.querySelector('[data-handoff-id]');
  return node ? node.dataset.handoffId : '';
}

const ACTIONS = {
  async import() {
    return request('/api/schemas/import', {
      files: ['schema_v2026_06.sql', 'schema_v2026_09.sql'],
    });
  },

  async run(button) {
    const body = {};
    if (button.dataset.forceStale) {
      body.base_version_seq = 0;
    }
    const run = await request('/api/runs', body);
    return run.reused
      ? `命中已有批次 ${run.run_id}`
      : `新批次 ${run.run_id} 序号 ${run.version_seq}`;
  },

  async 'compare-types'() {
    const oldValue = document.getElementById('old-type');
    const newValue = document.getElementById('new-type');
    const result = await request('/api/types/compare', {
      old: oldValue ? oldValue.value : '',
      new: newValue ? newValue.value : '',
    });
    const target = document.querySelector('[data-compare-result]');
    if (target) {
      const detail = result.detail ? `/${result.detail}` : '';
      target.textContent = `${result.old.norm} → ${result.new.norm}　${result.change}${detail}　${result.breaking ? '破坏' : '兼容'}`;
    }
    return `比较完成 ${result.change}`;
  },

  async 'handoff-recall'(button) {
    const body = { expected_revision: Number(button.dataset.expected) };
    const result = await request(`/api/handoffs/${handoffId()}/recall`, body);
    return `已撤回，版本 ${result.revision}，待结算 ${result.rollback.outstanding.length} 条`;
  },

  async 'handoff-claim'(button) {
    const purpose = button.dataset.purpose || 'execute';
    const body = {
      expected_revision: Number(button.dataset.expected),
      purpose,
    };
    const result = await request(
      `/api/handoffs/${handoffId()}/tasks/${button.dataset.task}/claim`, body);
    HANDOFF_CREDENTIALS.set(result.task_id, purpose, result.credential);
    return `已领取 ${result.task_id}，${result.credential.expires_at} 前有效`;
  },

  async 'handoff-receipt'(button) {
    const purpose = button.dataset.purpose || 'execute';
    const credential = HANDOFF_CREDENTIALS.get(button.dataset.task, purpose);
    if (!credential) {
      throw new Error('本页没有该任务的凭据，先领取再回执');
    }
    const result = await request(
      `/api/handoffs/${handoffId()}/tasks/${button.dataset.task}/receipt`, {
        credential,
        outcome: button.dataset.outcome,
      });
    if (result.accepted === false && result.conflict) {
      throw new Error(`回执留作冲突：${result.conflict.code}`);
    }
    return `回执已登记，任务 ${result.status}`;
  },
};

async function request(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const message = (payload && (payload.message || payload.error)) || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload;
}

function flash(target, message, bad) {
  if (!target) return;
  target.textContent = message;
  target.classList.toggle('is-bad', Boolean(bad));
  target.classList.toggle('is-ok', !bad);
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const name = button.dataset.action;
  const handler = ACTIONS[name];
  if (!handler) return;
  const target = document.querySelector('[data-flash]');
  button.disabled = true;
  flash(target, '…', false);
  try {
    const message = await handler(button);
    flash(target, message || '完成', false);
    if (name === 'run' || name.startsWith('handoff-')) {
      setTimeout(() => window.location.reload(), 400);
    }
  } catch (error) {
    flash(target, error.message, true);
  } finally {
    button.disabled = false;
  }
});
