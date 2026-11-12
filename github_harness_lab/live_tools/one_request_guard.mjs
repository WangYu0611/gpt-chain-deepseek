// Trusted local policy for the inspected official DSH 0.2.1-rc.3 composition.
// Public Cordis/DSH hooks only. No provider protocol, credentials, or HTTP client.
import { writeFileSync } from 'node:fs';

export const name = 'bounded-smoke-policy';
export const inject = ['llm', 'tools', 'agents'];

export function createPolicy(config, write = () => {}) {
  if (config.provider !== 'deepseek-account' || config.model !== 'deepseek-flash' ||
      config.maxRequests !== 1 || config.maxTokens !== 512 ||
      config.maxInputBytes !== 8192 || typeof config.preflightOnly !== 'boolean') {
    throw new Error('Unsupported bounded smoke policy');
  }
  const state = {
    protocol: 'bounded-dsh/v1', ready: true, preflight_only: config.preflightOnly,
    preflight_validated: false, provider: config.provider, model: config.model,
    request_count: 0, max_requests: 1, max_tokens: 512, max_input_bytes: 8192,
    observed_input_bytes: 0, visible_tool_count: null, tool_calls_blocked: 0,
    usage: null, finish_kind: null, violations: [],
  };
  const save = () => write(structuredClone(state));
  const deny = (reason) => {
    state.violations.push(reason); save();
    throw new Error(`BOUNDED_DSH: ${reason}`);
  };
  const inspect = (options) => {
    if (config.preflightOnly) deny('model dispatch in preflight');
    if (state.request_count !== 0) deny('second model request refused');
    if (options.provider !== config.provider || options.model !== config.model)
      deny('unexpected model route');
    if (options.maxTokens !== config.maxTokens || options.reasoningEffort !== 'off')
      deny('unexpected output/reasoning limit');
    if ((options.tools ?? []).length !== 0) deny('model-visible tools refused');
    if (!Array.isArray(options.messages) || options.messages.length === 0)
      deny('empty model request');
    for (const message of options.messages) {
      if (!['system', 'developer', 'user'].includes(message.role) ||
          !Array.isArray(message.content) ||
          message.content.some(block => block.type !== 'text' || typeof block.text !== 'string'))
        deny('non-text or resumed model request refused');
    }
    const inputBytes = Buffer.byteLength(JSON.stringify(options.messages), 'utf8');
    if (inputBytes > config.maxInputBytes) deny('input byte limit exceeded');
    state.observed_input_bytes = inputBytes;
    state.visible_tool_count = 0;
    state.request_count = 1; save();
  };
  const observe = (chunk) => {
    if (chunk.type === 'usage') {
      const clean = {};
      for (const key of ['inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens', 'totalTokens']) {
        const value = chunk.usage?.[key];
        if (typeof value === 'number' && Number.isFinite(value) && value >= 0) clean[key] = value;
      }
      state.usage = clean; save();
    }
    if (chunk.type === 'finish') {
      state.finish_kind = chunk.reason?.kind ?? chunk.kind ?? null; save();
    }
  };
  save();
  return { state, save, deny, inspect, observe };
}

export function apply(ctx, config) {
  if (typeof config.reportPath !== 'string' || !config.reportPath)
    throw new Error('A local guard report path is required');
  const policy = createPolicy(config, state =>
    writeFileSync(config.reportPath, JSON.stringify(state, null, 2), { encoding: 'utf8', mode: 0o600 }));
  ctx.tools.guard(() => {
    policy.state.tool_calls_blocked++;
    policy.state.violations.push('tool execution refused'); policy.save();
    return 'This bounded smoke run has no tool execution capabilities';
  });
  ctx.on('agent/created', ({ agent }) => {
    agent.ctx.tools.restrict({ allow: [] });
  });
  ctx.on('agent/pre-step', async ({ agent }, next) => {
    const schemas = ctx.tools.schemas(agent);
    policy.state.visible_tool_count = schemas.length;
    if (schemas.length !== 0) policy.deny('visible tool schemas at pre-step');
    if (config.preflightOnly) {
      policy.state.preflight_validated = true; policy.save();
      return { kind: 'reject' };
    }
    policy.save();
    return next();
  });
  ctx.on('agent/request', async (_payload, next) => {
    if (config.preflightOnly || policy.state.request_count !== 0)
      policy.deny('additional agent request refused before model discovery');
    const proposed = await next();
    return { ...proposed, provider: config.provider, model: config.model,
      reasoningEffort: 'off', maxTokens: config.maxTokens };
  });
  ctx.on('llm/stream', (options, next) => {
    policy.inspect(options);
    return (async function* () {
      for await (const chunk of next()) {
        policy.observe(chunk);
        yield chunk;
      }
    })();
  });
  // Runner explicitly injects this service, so it cannot outrun the guard.
  ctx.provide('boundedSmokePolicy', { protocol: 'bounded-dsh/v1' });
}
