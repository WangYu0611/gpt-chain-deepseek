(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const phases = [['PLAN','任务规划'],['CODE','代码实现'],['CHECK','独立检查'],['REVIEW','任务验收'],['PUBLISH','任务发布'],['MILESTONE_REVIEW','里程碑验收'],['MILESTONE_PUBLISH','里程碑发布']];
  const phaseNames = Object.fromEntries([...phases,['REPLAN','Claude重新分析修复计划'],['DONE','已完成'],['BLOCKED','已阻塞']]);
  const nextSteps = {
    PLAN:['发布任务书','规划者拟定范围和验收条件，发布后交给编码器。'],
    REPLAN:['继续同任务修复','Claude 返回修复计划后，继续开发并重新运行独立检查与审核。'],
    CODE:['提交后开始独立检查','本地提交进入构建和测试。'],
    CHECK:['检查结束后交给 Codex','验收会依据本轮检查和提交内容。'],
    REVIEW:['按验收结论继续','通过后登记任务，需修改时交回编码器。'],
    PUBLISH:['规划下一项任务','本任务的验收结果正在登记。'],
    MILESTONE_REVIEW:['按里程碑验收结论继续','完成全部检查后才能统一发布。'],
    MILESTONE_PUBLISH:['完成本次里程碑','发布结果会记录到运行状态。']
  };
  const errorLabels = {
    'ProviderError: Agent output has invalid core_tests':'任务书的核心测试清单格式无效，请检查完整测试名、数量及重复项。',
    'ProviderError: Provider exceeded its time or log limit':'上次执行达到时限或日志上限。',
    'ProviderError: Provider exceeded its time limit':'上次执行达到时限。',
    'ProviderError: Provider exceeded its log limit':'上次执行达到日志上限。'
  };
  let data = null, receivedAt = 0, busy = false, connected = false, lastError = '', toastTimer;
  const text = (id,value) => {
    const el=$(id),label=value == null || value === '' ? '—' : String(value);
    if(el.textContent!==label)el.textContent=label;
  };
  const str = value => typeof value === 'string' ? value : value == null ? '' : typeof value === 'object' ? JSON.stringify(value,null,2) : String(value);
  const number = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value) : null;
  const node = (tag,cls,value) => {const el=document.createElement(tag);if(cls)el.className=cls;if(value!=null)el.textContent=str(value);return el;};
  const parsed = value => {if(!value)return null;const date=new Date(value);return Number.isNaN(date.getTime())?null:date;};
  const dateTime = (value,full=false) => {const date=parsed(value);return date?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',...(full?{year:'numeric'}:{}),month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(date):str(value)||'—';};
  const duration = value => {const sec=number(value);if(sec===null)return '—';const s=Math.max(0,Math.floor(sec));if(s<60)return s+' 秒';if(s<3600)return Math.floor(s/60)+' 分 '+s%60+' 秒';if(s<86400)return Math.floor(s/3600)+' 小时 '+Math.floor(s%3600/60)+' 分';return Math.floor(s/86400)+' 天 '+Math.floor(s%86400/3600)+' 小时';};
  const shortSHA = value => str(value).slice(0,10) || '—';
  const age = (timestamp,fallback) => {const date=parsed(timestamp);if(date)return Math.max(0,(Date.now()-date.getTime())/1000);const n=number(fallback);return n===null?null:n+(Date.now()-receivedAt)/1000;};
  const setTone = (id,tone,base='') => {$(id).className=[base,tone].filter(Boolean).join(' ');};
  const phaseElements = new Map();
  for(const [index,[key,label]] of phases.entries()){
    const li=node('li'),number=node('span','phase-number',index+1);
    number.setAttribute('aria-hidden','true');
    li.append(number,node('span','',label));$('phase-list').append(li);phaseElements.set(key,li);
  }
  const renderedLists = new Map();
  function renderList(id,items,build){
    const fingerprint=JSON.stringify(items);
    if(renderedLists.get(id)===fingerprint)return;
    $(id).replaceChildren(...items.map(build));
    renderedLists.set(id,fingerprint);
  }
  function stepRunning(s,w){
    return ['plan','replan','code','check','review','publish','milestone_review','milestone_publish'].includes(s.code)
      &&!!s.started_at&&!s.attempt_finished&&!s.paused&&w.alive===true&&w.healthy===true;
  }
  function nextAction(s,w,sch,phase,codingName,planningName){
    switch(s.code){
      case 'waiting_provider': return ['到时自动重试','恢复后继续'+phase+'。'];
      case 'waiting_filesystem': return ['自动重试状态文件','文件可用后继续'+phase+'。'];
      case 'waiting_window': return ['按排期自动继续',sch.next_open?'下次开放 '+dateTime(sch.next_open)+'（北京）':'等待工作窗口开放。'];
      case 'paused': return ['等待恢复指令','恢复后从'+phase+'继续。'];
      case 'blocked': return ['处理阻塞后继续','本轮原因见当前任务下方。'];
      case 'stopped': return ['等待后台启动','断点保留在'+phase+'。'];
      case 'heartbeat_stale': return ['等待心跳确认',w.label||'后台状态待核实。'];
      case 'done': return ['当前运行已结束','可查看下方验收记录。'];
      case 'error': return ['核对运行状态','具体原因见当前任务下方。'];
      case 'not_initialized': return ['等待执行器创建断点','生成后会自动显示。'];
      case 'plan': return [nextSteps.PLAN[0],planningName+' 拟定范围和验收条件，发布后交给编码器。'];
      case 'replan': return nextSteps.REPLAN;
      case 'code': return [nextSteps.CODE[0],codingName+' 完成本地提交后进入构建和测试。'];
      default: return nextSteps[s.phase]||['等待状态更新','后台正在同步运行状态。'];
    }
  }
  function renderOverviewSteps(snapshot){
    const workflow=snapshot.workflow||{};
    const nodes=Array.isArray(workflow.nodes)?workflow.nodes:[];
    const evidence=snapshot.flows?.development?.nodes||[];
    const current=workflow.current;
    const useCritical=current==='critical_plan'||(current!=='routine_plan'&&workflow.route==='critical');
    const stageIds={plan:useCritical?'critical_plan':'routine_plan',code:'code',check:'check',
      review:current==='milestone_review'?current:'review',publish:current==='milestone_publish'?current:'publish'};
    const stageLabels={plan:'规划',code:'开发实现',check:'独立检查',review:'任务审核',publish:'任务登记'};
    for(const button of $('overview-steps').querySelectorAll('button')){
      const slot=button.dataset.overviewSlot,target=stageIds[slot];
      const item=nodes.find(row=>row.id===target);
      const state=item?.state||'idle';
      button.dataset.overviewNode=target;
      button.dataset.state=connected?state:'stale';
      button.querySelector('strong').textContent=item?.title||stageLabels[slot];
      button.querySelector('span').textContent=connected?(item?.state_label||moduleStates[state]):'数据过期';
      if(item&&target===workflow.current&&connected)button.setAttribute('aria-current','step');
      else button.removeAttribute('aria-current');
      button.disabled=!connected||!evidence.some(row=>row.id===target&&row.detail_url);
    }
  }
  const flowElements = new Map([...document.querySelectorAll('[data-node]')].map(el=>[el.dataset.node,el]));
  const moduleIcons = {routine_planner:'≡',critical_planner:'◇',developer:'⌘',checks:'✓',reviewer:'◎',publisher:'↗'};
  const moduleStates = {running:'正在执行',ready:'等待执行',waiting:'等待恢复',paused:'已暂停',stale:'状态待核实',blocked:'需要处理',completed:'本轮已完成',idle:'待命',disabled:'未启用',not_required:'本轮无需'};
  let selectedModule = '';
  function prettyModel(value){
    const name=str(value);
    return {'claude-sonnet-5-5':'Sonnet 5.5','claude-opus-5-5':'Opus 5.5','claude-fable-5-2':'Fable 5.2','gpt-6-astra':'GPT-6 Astra','gpt-6.2-astra':'GPT-6.2 Astra','gpt-6.2-sol':'GPT-6.2 Sol'}[name]||name;
  }
  function modelWithEffort(model,effort){
    return model?[prettyModel(model),effort].filter(Boolean).join(' · '):'';
  }
  function configuredModelLabel(item){
    const value=modelWithEffort(item.configured_model,item.configured_effort);
    return value?'当前配置 · '+value:'';
  }
  function completedModelLabel(item){
    const value=item.state==='completed'&&item.model_source==='recorded'?modelWithEffort(item.model,item.effort):'';
    return value?'已完成调用 · '+value:'';
  }
  function moduleModel(item){
    const configured=configuredModelLabel(item),completed=completedModelLabel(item);
    if(completed)return configured||completed;
    if(item.model)return modelWithEffort(item.model,item.effort)+(item.model_source==='configured'?' · 配置':'');
    if(configured)return configured+'（执行模型未记录）';
    return ['check','checks'].includes(item.id)?'UE / 自动化检查':['publish','publisher'].includes(item.id)?'本地调度器':'执行模型未记录';
  }
  function renderWorkflow(snapshot){
    const workflow=snapshot.workflow||{},nodes=Array.isArray(workflow.nodes)?workflow.nodes:[];
    const byId=new Map(nodes.map(item=>[item.id,item]));
    const current=byId.get(workflow.current),edges=workflow.edges||[];
    const graph=$('workflow-panel').querySelector('.dev-flow');
    graph.dataset.route=workflow.route||'';
    const successors={routine_plan:workflow.route==='critical'?'critical_plan':'code',critical_plan:'code',
      code:'check',check:'review',review:'publish'};
    for(const [id,el] of flowElements){if(!byId.has(id)){
      el.dataset.state='idle';delete el.dataset.link;el.removeAttribute('aria-current');
      const state=el.querySelector('[data-field="state"]');if(state)state.textContent='等待同步';
      const model=el.querySelector('[data-field="model"]');if(model)model.textContent='执行模型待记录';
    }}
    text('flow-current',current?current.title+' · '+(current.state_label||moduleStates[current.state]||'待同步'):'等待流程数据');
    $('flow-current').dataset.state=current?.state||'idle';
    for(const item of nodes){
      const el=flowElements.get(item.id);if(!el)continue;
      el.dataset.state=item.state||'idle';
      const successor=byId.get(successors[item.id]);
      const edge=successor&&edges.find(link=>link.from===item.id&&link.to===successor.id);
      el.dataset.link=edge?.active?'active':edge&&item.state==='completed'&&successor.state==='completed'?'complete':'idle';
      const state=el.querySelector('[data-field="state"]');if(state)state.textContent=item.state_label||moduleStates[item.state]||'待同步';
      const model=el.querySelector('[data-field="model"]');
      if(model)model.textContent=moduleModel(item);
      const title=el.querySelector('.flow-title');if(title)title.textContent=item.title||title.textContent;
      el.title=[item.summary||item.title,completedModelLabel(item)].filter(Boolean).join('\n');
      if(item.current)el.setAttribute('aria-current','step');else el.removeAttribute('aria-current');
    }
    graph.dataset.branch=workflow.route==='critical'?flowElements.get('routine_plan').dataset.link||'idle':'idle';
    const milestone=$('pipeline-milestone'),flowLane=snapshot.flows?.development?.nodes||[];
    milestone.hidden=!['milestone_review','milestone_publish'].some(id=>byId.has(id)&&byId.get(id).enabled!==false);
    for(const button of milestone.querySelectorAll('button'))button.disabled=!flowLane.some(item=>item.id===button.dataset.flowNode);
    const pending=snapshot.publication?.pending_tasks;
    text('pipeline-milestone-count',Number.isInteger(pending)?pending+' 项待阶段发布':'');
    const routeReason=str(workflow.route_reason);
    text('flow-branch-caption',snapshot.status?.phase==='REPLAN'?'正在重新规划同一任务的返工，然后继续开发和独立检查。':routeReason||(workflow.route==='critical'?'关键规划完成后进入开发。':workflow.route==='routine'?'常规规划完成后直接进入开发。':'规划路径待确认'));
    $('flow-branch-caption').title=$('flow-branch-caption').textContent;
    const modules=(Array.isArray(snapshot.modules)?snapshot.modules:[]).filter(item=>Object.hasOwn(moduleIcons,item.id));
    const openModules=new Set([...$('modules-grid').querySelectorAll('details[open]')].map(el=>el.dataset.module));
    text('modules-count',modules.length?modules.filter(item=>item.state==='running').length+' 个模块执行中 · '+modules.length+' 个职责模块':'等待模块数据');
    renderList('modules-grid',modules,item=>{
      const card=node('article','role-card'+(selectedModule===item.id?' selected':''));card.id='module-'+item.id;card.dataset.module=item.id;card.dataset.state=item.state||'idle';
      const top=node('div','role-top'),identity=node('div','role-identity');identity.append(node('span','role-icon',moduleIcons[item.id]),node('h3','',item.title));
      top.append(identity,node('span','module-status',item.state_label||moduleStates[item.state]||'待同步'));
      const model=node('div','role-model',moduleModel(item));
      const summary=node('div','role-summary');
      if(item.current&&item.task_title)summary.append(node('strong','role-task',str(item.task_title).replace(/^TASK-[A-Za-z0-9-]+[：:\s]*/,'')));
      summary.append(node('div','',item.summary||'等待执行记录。'));
      card.append(top,model,node('p','role-duty',item.responsibility),summary);
      const details=node('details','role-details');details.dataset.module=item.id;details.open=openModules.has(item.id)||selectedModule===item.id;
      details.append(node('summary','','查看模块记录'));
      const completed=completedModelLabel(item);
      if(completed&&item.configured_model)details.append(node('p','small role-recorded-model',completed));
      const activity=Array.isArray(item.activity)?item.activity:[];
      if(activity.length){const list=node('ul','role-events');list.append(...activity.map(event=>node('li','',event.text||'执行记录')));details.append(list);}
      else details.append(node('p','small',item.current?'当前尚无可展示的工具记录。':'执行到该模块时显示对应活动。'));
      if(item.task_id)details.append(node('p','small','对应任务 '+str(item.task_id)));
      card.append(details);return card;
    });
    const active=modules.find(item=>item.current);
    text('actor-model',active?moduleModel(active):'');
    text('metric-accepted',Array.isArray(snapshot.accepted)?snapshot.accepted.length:'—');
    text('metric-pending',snapshot.publication?.pending_tasks??'—');
    text('metric-reworks',snapshot.task?.reworks??'—');
    const planning=snapshot.task?.planning||{},task=snapshot.task||{};
    const level=planning.level||task.planning_level;
    const configured=configuredModelLabel(planning),recorded=modelWithEffort(planning.model||task.planning_model,planning.effort);
    text('task-planning-level',level==='critical'?'关键系统规划':level==='routine'?'日常任务规划':'规划分级未记录');
    text('task-planning-model',configured||(recorded?'已完成规划 · '+recorded:'规划模型未记录'));
    const history=configured&&recorded&&recorded!==modelWithEffort(planning.configured_model,planning.configured_effort)?'已完成规划 · '+recorded:'';
    $('task-planning-history').hidden=!history;text('task-planning-history',history);
    text('task-planning-reason',planning.reason||task.planning_reason||'');
  }
  function developmentState(snapshot){
    const s=snapshot.status||{},wf=snapshot.workflow||{},current=(wf.nodes||[]).find(n=>n.id===wf.current);
    const state=!connected?'stale':current?.state||({blocked:'blocked',paused:'paused',heartbeat_stale:'stale',stopped:'waiting',waiting_provider:'waiting',waiting_filesystem:'waiting',waiting_window:'waiting'}[s.code]||'idle');
    return {current,state,label:!connected?'连接中断 · 数据过期':current?.state_label||moduleStates[state]||'等待同步'};
  }
  const routeCards=new Map([...document.querySelectorAll('[data-route-step]')].map(card=>[card.dataset.routeStep,card]));
  function renderReviewRoute(snapshot){
    const route=snapshot.review_route,panel=$('dev-review-route');
    panel.hidden=!route;
    if(!route)return;
    const slots=Array.isArray(route.slots)?route.slots:[];
    const primary=slots.find(slot=>slot.slot===1);
    const backups=slots.filter(slot=>slot.slot>1);
    const backupState=backups.some(slot=>slot.status==='completed')?'completed':
      backups.some(slot=>slot.status==='running')?'running':
      backups.length&&backups.every(slot=>slot.status==='failed')?'failed':'waiting';
    function showStep(key,state,detail){
      const card=routeCards.get(key),effectiveState=!connected&&state==='running'?'stale':state;
      card.dataset.state=effectiveState;
      card.querySelector('.route-step-mark').textContent={running:'●',completed:'✓',failed:'!',stale:'–',not_required:'–'}[effectiveState]||'○';
      card.querySelector('p').textContent=effectiveState==='stale'?'连接中断 · 状态待同步':detail;
    }
    const primaryState=primary?.status||'waiting',latestBackup=backups.find(slot=>slot.status==='running'||slot.status==='completed');
    const unavailable=backups.filter(slot=>slot.status==='failed').length;
    const primaryDetail={running:'正在用主 Key 核验',completed:'本轮 API 核验已完成',failed:primary?.failure_label||'本轮不可用'}[primaryState]||
      (route.fallback_reason==='no_credentials'?'本轮没有可用 Key':'等待核验');
    const backupDetail={running:'正在使用备用 '+(latestBackup?.slot-1),completed:'备用 '+(latestBackup?.slot-1)+' 已完成核验',
      failed:unavailable+' 把备用 Key 本轮不可用'}[backupState]||
      (route.fallback_reason==='no_credentials'?'本轮没有备用 Key':
        primaryState==='completed'?'本轮未启用':backups.length?backups.length+' 把可轮试':'主 Key 不可用时轮试');
    showStep('primary',primaryState,primaryDetail);
    showStep('backups',primaryState==='completed'||route.fallback_reason==='no_credentials'?'not_required':backupState,backupDetail);
    const native=route.subscription_status||'waiting';
    const nativeDetail={running:'已接手同一提交的核验',completed:'订阅核验已完成',failed:'订阅核验调用失败'}[native]||
      (route.api_status==='completed'?'本轮未启用':route.fallback_reason?'正在切换到订阅':'API 不可用时接手');
    $('dev-review-steps').dataset.fallback=String(!!route.fallback_enabled);
    routeCards.get('subscription').hidden=!route.fallback_enabled;
    showStep('subscription',route.api_status==='completed'?'not_required':native,nativeDetail);
    panel.dataset.current=String(connected&&(route.api_status==='running'||native==='running'));
    const model=route.model?[prettyModel(route.model),route.effort].filter(Boolean).join(' · '):'';
    $('dev-review-model').hidden=!model;text('dev-review-model',model);
    const head=route.head_sha||(['REVIEW','ART_REVIEW'].includes(snapshot.status?.phase)&&route.api_status!=='waiting'?snapshot.task?.head_sha:null);
    $('dev-review-head').hidden=!head;text('dev-review-head',head?'提交 '+shortSHA(head):'');
    $('dev-review-head').title=str(head);
    const apiNote={running:'Responses API 正在核验。',completed:'Responses API 已完成核验。',failed:'Responses API 核验请求失败。'}[route.api_status];
    const note=!connected?'连接中断，显示上次同步的核验记录。':route.failure_label||(!slots.length?apiNote:'')||'';
    $('dev-review-note').hidden=!note;text('dev-review-note',note);
  }
  const checkNames={'ue-editor-build':'UE 编辑器编译','ue-task-validation':'任务专属验证'};
  function checkName(id){return checkNames[id]||str(id)||'未命名检查';}
  function renderCheckRows(checks,currentId){
    const rows=currentId&&!checks.some(check=>check.id===currentId)?[...checks,{id:currentId,status:'running'}]:checks;
    $('checks-empty').hidden=rows.length>0;
    renderList('checks',rows,check=>{
      const result=check.status==='running'?'running':check.status==='not_run'?'skipped':
        check.passed===true?'passed':check.passed===false?'failed':'unknown';
      const [label,tone]={running:['正在检查','good'],skipped:['未运行','warn'],passed:['通过','good'],
        failed:['未通过','bad'],unknown:['待定','muted']}[result];
      const row=node('div','check'+(result==='running'?' current':''));
      row.dataset.result=result;
      const name=node('span','check-id',checkName(check.id));name.title=str(check.id);
      row.append(name,node('span','check-result '+tone,label+(check.exit_code!=null&&check.exit_code!==0?' · 退出码 '+check.exit_code:'')));
      if(check.reason==='editor_build_failed')row.append(node('span','check-reason','等待编辑器编译通过'));
      return row;
    });
  }
  function renderDevelopment(snapshot){
    renderUserRepair(snapshot);
    renderFullFlowTest(snapshot);
    renderReviewRoute(snapshot);
    const priority=$('dev-priority-evidence'),history=$('dev-historical-evidence');
    const blocked=snapshot.status?.phase==='BLOCKED'||snapshot.status?.code==='blocked';
    const tests=snapshot.full_flow_test;
    for(const [id,showFirst] of [['user-repair-panel',blocked||!!snapshot.task?.user_repair],
      ['full-flow-test',blocked||!!snapshot.full_flow_error||!!tests&&!tests.historical]]){
      const card=$(id),container=showFirst?priority:history;
      if(card.parentElement!==container)container.append(card);
    }
    priority.hidden=!Array.from(priority.children).some(card=>!card.hidden);
    history.hidden=!Array.from(history.children).some(card=>!card.hidden);
    const task=snapshot.task,wf=snapshot.workflow||{},s=snapshot.status||{},mods=snapshot.modules||[];
    const {current,state,label}=developmentState(snapshot);
    const title=task?str(task.title||task.id).replace(/^TASK-[A-Za-z0-9-]+[：:\s]*/,''):
      s.code==='done'?'当前运行已完成':s.phase==='PLAN'&&state==='running'?'正在拟定下一项任务':'等待下一项开发任务';
    $('dev-task-id').hidden=!task?.id;text('dev-task-id',task?.id||'');
    text('dev-task-title',title);$('dev-task-title').title=title;
    text('dev-task-summary',!connected?'连接中断，以下保留最后一次同步的任务和执行证据。':s.summary||'新任务启动后，将显示任务目标与执行状态。');
    text('dev-state',label);$('dev-state').dataset.state=state;
    const worker=snapshot.worker||{};
    text('dev-worker-health',!connected?'后台状态待同步':worker.alive===false?'后台已停止':worker.alive===true?(worker.healthy===true?'后台在线':'后台心跳待核实'):'后台状态未知');
    $('dev-worker-health').classList.toggle('worker-offline',connected&&worker.alive===false);
    text('dev-stage-name',current?.title||phaseNames[s.phase]||'等待任务');
    const activeModule=mods.find(m=>m.id===current?.module_id)||mods.find(m=>m.current);
    text('dev-model',activeModule?moduleModel(activeModule):'执行模型未记录');
    text('dev-route',task?.user_repair?'用户授权的全流程修复':s.phase==='REPLAN'?'返工重规划':wf.route==='critical'?'关键规划路径':wf.route==='routine'?'常规规划路径':'规划路径待确认');
    text('dev-reworks',task?.reworks??0);text('dev-pending-publish',snapshot.publication?.pending_tasks??'—');
    const rework=wf.rework||{};$('flow-rework').dataset.active=String(!!rework.active);
    $('flow-rework').title=rework.reason||'审核要求修改时，返回同一任务继续开发。';
    text('flow-rework',rework.active?'第 '+str(rework.count)+' 次返工 · '+(s.phase==='REPLAN'?'重新规划同一任务':'返回开发'):rework.count?'本任务已返工 '+str(rework.count)+' 次':'审核需修改时返回开发');
    const selected=mods.find(m=>m.id===selectedModule)||activeModule||mods[0];
    for(const card of $('modules-grid').children){
      card.hidden=card.dataset.module!==selected?.id;card.classList.toggle('selected',!card.hidden);
      if(!card.hidden){card.tabIndex=-1;const status=card.querySelector('.module-status');if(status)status.textContent=connected?(selected.state_label||moduleStates[selected.state]):'数据过期';}
    }
    for(const button of document.querySelectorAll('.dev-flow .flow-node')){
      button.classList.toggle('selected',button.dataset.module===selected?.id);
      button.setAttribute('aria-pressed',String(button.dataset.module===selected?.id));
      const entry=(wf.nodes||[]).find(n=>n.id===button.dataset.node),label=button.querySelector('[data-field="state"]');
      if(label)label.textContent=!connected?'数据过期':entry?.id==='check'&&entry.state==='blocked'?'检查未通过':
        entry?.state_label||moduleStates[entry?.state]||'等待同步';
    }
    $('dev-follow-current').setAttribute('aria-pressed',String(!selectedModule));
    text('modules-count',selected?(selectedModule?'正在查看：':'跟随当前：')+selected.title:'等待模块执行记录');
    const events=[...(snapshot.activity||[])].sort((a,b)=>(parsed(b.at)?.getTime()||0)-(parsed(a.at)?.getTime()||0));
    text('dev-live-event',events[0]?.text||'当前调用尚无可展示活动。');text('dev-live-time',events[0]?.at?dateTime(events[0].at)+' · '+(events[0].actor||'执行记录'):'');
    const progress=snapshot.check_progress||{},old=progress.evidence_scope==='previous_attempt';
    const checks=Array.isArray(snapshot.checks)?snapshot.checks:[],total=Math.max(0,number(progress.total)||0);
    const relevant=old?[]:checks,executed=relevant.filter(c=>c.status!=='not_run'),passed=executed.filter(c=>c.passed===true).length,failed=executed.filter(c=>c.passed===false).length;
    const skipped=relevant.filter(c=>c.status==='not_run').length,recorded=relevant.length,pending=Math.max(0,total-recorded);
    const running=connected&&!old&&state==='running'&&current?.id==='check'&&!!progress.current_id&&!relevant.some(c=>c.id===progress.current_id);
    const recordedPassed=checks.filter(c=>c.passed===true&&c.status!=='not_run').length;
    const cause=checks.find(c=>c.id==='ue-editor-build'&&c.passed===false&&c.status!=='not_run')||
      checks.find(c=>c.passed===false&&c.status!=='not_run');
    const withheld=checks.filter(c=>c.status==='not_run').length;
    const causeText=cause?checkName(cause.id)+'未通过'+(cause.exit_code!=null&&cause.exit_code!==0?'（退出码 '+cause.exit_code+'）':'')+
      (withheld?'，'+withheld+' 项检查未运行。':'。'):'';
    const causeScope=old?'上一轮：':'本轮：';
    const nextCheck=connected&&old&&['CODE','REPLAN'].includes(s.phase)?' 修复提交后会重新检查。':'';
    const checkNote=(old?'上一轮 ':'')+causeText;
    $('dev-check-cause').hidden=!causeText;
    $('dev-check-cause').dataset.scope=old?'previous_attempt':'current';
    text('dev-check-cause',causeText?causeScope+causeText+nextCheck:'');
    $('overview-check-note').hidden=!causeText;
    text('overview-check-text',causeText?checkNote:'');
    text('checks-count',total?(old?'上一轮 · ':'')+recordedPassed+' / '+total+' 通过':'尚无结果');
    renderCheckRows(checks,running?progress.current_id:null);
    text('dev-check-count',passed+' / '+total);
    $('dev-check-metric-label').dataset.state=old?'previous_attempt':failed?'failed':'';
    text('dev-check-metric-label',old?'等待本轮独立检查':failed?failed+' 项未通过'+(skipped?' · '+skipped+' 项未运行':''):'本轮独立检查通过');
    text('dev-check-summary',old?'本轮通过 0 / '+total:passed+' / '+total+' 项通过');
    text('dev-check-scope',old?'下方为上一轮结果':recorded?'本轮检查记录':'尚未记录检查');setTone('dev-check-scope',old?'warn':'','tag');
    text('dev-check-pending',[failed?failed+' 项未通过':'',skipped?skipped+' 项未运行':'',running?'正在检查 '+checkName(progress.current_id):'',
      pending?'尚待记录 '+pending+' 项':'',old?'等待本轮新检查':''].filter(Boolean).join(' · ')||'检查已记录');
    const resultTypes=relevant.map(c=>c.status==='not_run'?'skipped':c.passed===true?'passed':c.passed===false?'failed':'unknown');
    if(running)resultTypes.push('running');
    while(resultTypes.length<Math.min(total,40))resultTypes.push('pending');
    const visibleResults=resultTypes.slice(0,40);
    renderList('dev-check-progress',visibleResults,result=>{const el=node('span','dev-check-segment');el.dataset.result=result;return el;});
    $('dev-check-progress').dataset.scope=old?'previous_attempt':'current';$('dev-check-progress').classList.toggle('is-stale',!connected);
    $('dev-check-progress').setAttribute('aria-label',(old?'本轮尚无结果；下方保留上一轮检查。':'本轮独立检查：')+
      '通过 '+passed+' / '+total+' 项，未通过 '+failed+' 项，未运行 '+skipped+' 项，尚待记录 '+pending+' 项。');
    const accepted=Array.isArray(snapshot.accepted)?snapshot.accepted:[];text('dev-recent-count',accepted.length);
    const recent=[...accepted].reverse().sort((a,b)=>(parsed(b.accepted_at)?.getTime()||0)-(parsed(a.accepted_at)?.getTime()||0)).slice(0,6);
    renderList('dev-recent-accepted',recent,item=>{
      const card=node('article','dev-recent-card'),head=node('div','dev-recent-head');head.append(node('span','hash',item.id),node('span','good','已验收'));
      const heading=node('h3','dev-recent-title',str(item.title||item.id).replace(/^TASK-[A-Za-z0-9-]+[：:\s]*/,''));heading.title=str(item.title||item.id);
      card.append(head,heading,node('div','dev-recent-meta',dateTime(item.accepted_at)+' · 返工 '+(item.reworks??0)+' 次'));return card;
    });
    if(!recent.length)$('dev-recent-accepted').replaceChildren(node('div','empty','尚无已验收任务。'));
    const publication=snapshot.publication||{};
    text('dev-delivery-label',publication.pending_tasks!=null?str(publication.pending_tasks)+' 项已验收任务等待阶段发布':'阶段交付状态待同步');
    text('dev-delivery-detail',publication.summary||'任务验收和阶段发布分别记录，具体发布策略待同步。');
    if(!connected)text('flow-current','连接中断 · 流程数据过期');
    updateDevelopmentTime();
  }
  function updateDevelopmentTime(){
    if(!data)return;const s=data.status||{},w=data.worker||{};
    text('dev-elapsed',connected&&stepRunning(s,w)?duration(age(s.started_at)):duration(s.elapsed_seconds));
  }
  $('dev-follow-current').addEventListener('click',()=>{selectedModule='';if(data)renderDevelopment(data);});

  for(const el of flowElements.values()){
    if(!el.dataset.module)continue;
    el.addEventListener('click',()=>{
      selectedModule=el.dataset.module;
      if(data)renderDevelopment(data);
      for(const card of $('modules-grid').children)card.classList.toggle('selected',card.dataset.module===selectedModule);
      const target=$('module-'+selectedModule);if(!target)return;
      const details=target.querySelector('details');if(details)details.open=true;
      target.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'center'});
      target.tabIndex=-1;target.focus({preventScroll:true});
    });
  }
  const viewLabels={overview:'工作台总览',development:'功能开发',art:'美术工作室',flows:'模块流程',history:'验收记录',settings:'运行设置'};
  const legacyViews={'art-panel':'art','workflow-panel':'development','modules-panel':'development','task-panel':'development','checks-panel':'development','activity-panel':'development','accepted-panel':'history','deferred-panel':'history','system-settings':'settings'};
  let activeView='',viewScroll=new Map();
  function routeView(){
    const hash=location.hash.slice(1),view=Object.hasOwn(viewLabels,hash)?hash:legacyViews[hash]||'overview';
    if(view===activeView)return;
    if(activeView)viewScroll.set(activeView,window.scrollY);
    const initial=!activeView;activeView=view;
    for(const key of Object.keys(viewLabels))$('view-'+key).hidden=key!==view;
    for(const link of document.querySelectorAll('.links [data-open-view]')){
      if(link.dataset.openView===view)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');
    }
    text('view-title',viewLabels[view]);document.title=viewLabels[view]+' · 无人值守工作台';
    if(legacyViews[hash]&&$(hash)){requestAnimationFrame(()=>$(hash).scrollIntoView({block:'start'}));}
    else if(!initial){$('view-title').focus({preventScroll:true});window.scrollTo(0,viewScroll.get(view)||0);}
  }
  window.addEventListener('hashchange',routeView);
  routeView();

  const flowLaneNames={development:'功能开发',art:'美术制作',operations:'后台服务'};
  let flowsFingerprint='';
  function flowModelLabel(item,{evidence=false}={}){
    const configured=configuredModelLabel(item),completed=completedModelLabel(item);
    if(completed)return evidence?[completed,configured].filter(Boolean).join(' / '):configured||completed;
    if(!item.model){
      if(configured)return configured+'（执行模型未记录）';
      const actor=str(item.actor);
      if(actor==='用户')return '人工视觉验收';
      if(/Tripo/i.test(actor))return '云端生成工具';
      if(/Blender|UE|Git|控制器|监督器|通知器|后台|飞书/i.test(actor))return '工具 / 服务执行';
      return '执行模型未记录';
    }
    const value=modelWithEffort(item.model,item.effort);
    return item.model_source==='recorded'?'执行模型 · '+value:item.model_source==='configured'?'配置模型 · '+value+'（执行模型未记录）':'模型来源未记录 · '+value;
  }
  function renderFlows(snapshot){
    const flows=snapshot.flows||{},fingerprint=JSON.stringify([flows,connected]);
    if(flowsFingerprint===fingerprint)return;flowsFingerprint=fingerprint;
    const focused=document.activeElement,focusLane=focused?.dataset.flowLane,focusNode=focused?.dataset.flowNode;
    const focusJump=focused?.dataset.flowJump,focusSection=focused?.classList.contains('flows-lane')?focused.id:null;
    const jumps=[];
    const lanes=Object.keys(flowLaneNames).map(key=>{
      const lane=flows[key]||{},all=Array.isArray(lane.nodes)?lane.nodes:[],edges=Array.isArray(lane.edges)?lane.edges:[];
      const nodes=all.filter(item=>!(key==='development'&&item.id==='critical_plan'&&item.state==='not_required'&&!item.current));
      const current=nodes.find(item=>item.id===lane.current||item.current);
      const label=lane.title||flowLaneNames[key],laneId='flow-lane-'+key;
      const jump=node('button','',label);
      jump.type='button';jump.dataset.flowJump=key;jump.dataset.state=connected?current?.state||'idle':'stale';
      jump.setAttribute('aria-controls',laneId);jumps.push(jump);

      const section=node('section','flows-lane');
      section.id=laneId;section.tabIndex=-1;section.setAttribute('aria-labelledby',laneId+'-title');
      const head=node('header','flows-lane-head'),heading=node('h2','',label);
      heading.id=laneId+'-title';head.append(heading);
      if(current){
        const status=node('div','flows-current');status.dataset.state=current.state;
        status.append(node('span','flows-current-state',flowStatusLabel(current)),node('strong','',current.title||current.id));
        head.append(status);
      }
      if(lane.summary&&lane.summary!==current?.title&&lane.summary!==current?.state_label)head.append(node('p','',lane.summary));

      const body=node('div','flows-lane-body'),timeline=node('ol','flows-nodes');
      const byId=new Map(nodes.map(item=>[item.id,item]));
      for(const [index,item] of nodes.entries()){
        const outgoing=edges.filter(edge=>edge.from===item.id&&byId.has(edge.to));
        const adjacent=outgoing.find(edge=>edge.to===nodes[index+1]?.id&&edge.kind!=='observe');
        const step=node('li','flows-step'),marker=node('span','flows-step-marker');marker.setAttribute('aria-hidden','true');
        step.dataset.state=item.state||'idle';step.dataset.linked=String(!!adjacent);
        step.dataset.activeEdge=String(!!adjacent?.active);
        const button=node('button','flows-node');
        button.type='button';button.dataset.flowLane=key;button.dataset.flowNode=str(item.id);
        button.dataset.state=step.dataset.state;button.dataset.current=String(item===current);
        if(item===current)button.setAttribute('aria-current','step');

        const name=node('span','flows-node-name'),context=node('span','flows-node-context'),detail=node('span','flows-node-detail');
        name.append(node('span','flows-node-title',item.title||item.id),node('span','flows-node-state',flowStatusLabel(item)));
        context.append(node('span','flows-node-actor',item.actor||'执行者未记录'));
        const modelLine=flowModelLabel(item);
        if(modelLine!=='工具 / 服务执行')context.append(node('span','flows-node-model',modelLine));
        if(item.blocked_reason)detail.append(node('span','flows-node-reason',item.blocked_reason));
        if(item.summary&&![item.title,item.state_label,item.blocked_reason].includes(item.summary)){
          detail.append(node('span','flows-node-summary',item.summary));
        }

        const transitions=outgoing.filter(edge=>edge!==adjacent||edge.label);
        if(transitions.length){
          const onward=node('span','flows-node-next',transitions.map(edge=>{
            const target=byId.get(edge.to);
            return [edge.label,'→',target.title||target.id].filter(Boolean).join(' ');
          }).join(' · '));
          onward.dataset.active=String(transitions.some(edge=>edge.active));detail.append(onward);
        }
        const arrow=node('span','flows-node-open','↗');arrow.setAttribute('aria-hidden','true');
        button.append(name,context,detail,arrow);button.title='查看'+str(item.title||item.id)+'的日志与产物';
        step.append(marker,button);timeline.append(step);
      }
      body.append(nodes.length?timeline:node('p','flows-empty','暂无流程记录'));
      if(all.some(item=>item.id==='critical_plan'&&!nodes.includes(item)))body.append(node('p','flows-scope-note','本轮无需关键规划'));
      section.append(head,body);
      return section;
    });
    $('flows-jumps').replaceChildren(...jumps);
    $('flows-lanes').replaceChildren(...lanes);
    if(!$('flow-detail-dialog').open){
      const retained=focusLane&&focusNode?findFlowButton(focusLane,focusNode):
        focusJump?jumps.find(jump=>jump.dataset.flowJump===focusJump):focusSection?$(focusSection):null;
      retained?.focus({preventScroll:true});
    }
  }
  function flowStatusLabel(item){return connected?(item.state_label||moduleStates[item.state]||'状态待核实'):'数据过期';}
  $('flows-jumps').addEventListener('click',event=>{
    const jump=event.target.closest('button[data-flow-jump]');
    if(!jump)return;
    const lane=$('flow-lane-'+jump.dataset.flowJump);
    lane.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});
    lane.focus({preventScroll:true});
  });
  function findFlowButton(lane,id){return [...$('flows-lanes').querySelectorAll('[data-flow-node]')].find(el=>el.dataset.flowLane===lane&&el.dataset.flowNode===id);}
  const flowDialog={selection:null,trigger:null,token:0,detailSequence:0,evidenceSequence:0,detailController:null,evidenceController:null,timer:null,objectURL:null,evidence:null,items:[],menuFingerprint:''};
  function flowURL(value,kind,selection){
    try{const url=new URL(value,location.href);return url.origin===location.origin&&url.pathname==='/api/flow/'+kind&&url.searchParams.get('lane')===selection.lane&&url.searchParams.get('node')===selection.node&&!url.username&&!url.password&&(kind!=='evidence'||url.searchParams.has('ref'))?url.href:null;}catch{return null;}
  }
  function releaseFlowImage(){if(flowDialog.objectURL){URL.revokeObjectURL(flowDialog.objectURL);flowDialog.objectURL=null;}$('flow-evidence-image').removeAttribute('src');$('flow-evidence-image').hidden=true;}
  function resetFlowEvidence(message='选择日志或产物以查看内容。'){
    releaseFlowImage();$('flow-evidence-text').hidden=false;text('flow-evidence-text',message);$('flow-evidence-download').hidden=true;$('flow-evidence-download').removeAttribute('href');$('flow-evidence-status').hidden=true;
  }
  function evidenceSize(bytes){const value=number(bytes);return value===null?'':value<1024?value+' B':value<1048576?(value/1024).toFixed(1)+' KB':(value/1048576).toFixed(1)+' MB';}
  function renderFlowEvidenceMenu(detail){
    const groups=[['flow-detail-logs',Array.isArray(detail.logs)?detail.logs:[],'暂无该步骤日志。'],['flow-detail-artifacts',Array.isArray(detail.artifacts)?detail.artifacts:[],'暂无该步骤产物。']];
    flowDialog.items=groups.flatMap(([,items])=>items);
    const fingerprint=JSON.stringify(groups);if(fingerprint===flowDialog.menuFingerprint)return;flowDialog.menuFingerprint=fingerprint;
    const focusedURL=document.activeElement?.dataset.evidenceUrl;
    for(const [id,items,empty] of groups){
      $(id).replaceChildren(...(items.length?items.map(item=>{
        const li=node('li'),button=node('button','flow-evidence-choice',item.title||item.id||'查看证据');button.type='button';button.dataset.evidenceUrl=str(item.url);button.setAttribute('aria-pressed',String(item.url===flowDialog.evidence?.url));
        if(evidenceSize(item.size_bytes))button.append(node('span','flow-evidence-size',evidenceSize(item.size_bytes)));
        button.disabled=!flowURL(item.url,'evidence',flowDialog.selection);button.addEventListener('click',()=>selectFlowEvidence(item));li.append(button);return li;
      }):[node('li','empty',empty)]));
    }
    if(focusedURL)[...$('flow-detail-dialog').querySelectorAll('.flow-evidence-choice')].find(button=>button.dataset.evidenceUrl===focusedURL)?.focus({preventScroll:true});
  }
  async function loadFlowEvidence(item,token=flowDialog.token){
    const selection=flowDialog.selection,url=selection&&flowURL(item.url,'evidence',selection);if(!url)return;
    flowDialog.evidenceController?.abort();const controller=new AbortController(),sequence=++flowDialog.evidenceSequence;flowDialog.evidenceController=controller;
    const timeout=setTimeout(()=>controller.abort(),7000),isCurrent=()=>token===flowDialog.token&&sequence===flowDialog.evidenceSequence&&$('flow-detail-dialog').open&&flowDialog.evidence?.url===item.url;
    text('flow-evidence-status','正在同步证据…');$('flow-evidence-status').hidden=false;
    try{
      const response=await fetch(url,{cache:'no-store',signal:controller.signal,headers:{Accept:'text/plain, image/png, application/octet-stream'}});if(!response.ok)throw new Error('HTTP '+response.status);
      const mime=(response.headers.get('Content-Type')||'').split(';')[0].trim().toLowerCase();
      const isImage=mime==='image/png',isText=mime.startsWith('text/')||mime==='application/json'||mime.endsWith('+json');
      const body=isImage?await response.blob():isText?await response.text():null;
      const value=isImage?await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error('图片内容无法读取'));reader.readAsDataURL(body);}):body;if(!isCurrent())return;
      const previousScroll=$('flow-evidence-text').scrollTop;releaseFlowImage();$('flow-evidence-download').href=url;$('flow-evidence-download').hidden=false;
      $('flow-evidence-text').hidden=isImage;
      if(isImage){$('flow-evidence-image').src=value;$('flow-evidence-image').alt=str(item.title||'步骤图片产物');$('flow-evidence-image').hidden=false;}
      else{text('flow-evidence-text',isText?value:'该产物为二进制文件，请下载原文件查看。');$('flow-evidence-text').scrollTop=previousScroll;}
      $('flow-evidence-status').hidden=true;
    }catch(error){if(isCurrent()){text('flow-evidence-status','证据数据过期，无法同步；保留已读取内容。'+(error.name==='AbortError'?'请求超时。':str(error.message)));$('flow-evidence-status').hidden=false;}}
    finally{clearTimeout(timeout);if(sequence===flowDialog.evidenceSequence)flowDialog.evidenceController=null;}
  }
  function selectFlowEvidence(item){
    flowDialog.evidence=item;resetFlowEvidence('正在读取证据…');text('flow-evidence-title',item.title||item.id||'步骤证据');
    for(const button of $('flow-detail-dialog').querySelectorAll('.flow-evidence-choice'))button.setAttribute('aria-pressed',String(button.dataset.evidenceUrl===item.url));
    loadFlowEvidence(item);
  }
  async function loadFlowDetail(){
    const selection=flowDialog.selection;if(!selection||!$('flow-detail-dialog').open||flowDialog.detailController)return;
    const token=flowDialog.token,sequence=++flowDialog.detailSequence,controller=new AbortController();flowDialog.detailController=controller;
    const timeout=setTimeout(()=>controller.abort(),7000),isCurrent=()=>token===flowDialog.token&&sequence===flowDialog.detailSequence&&$('flow-detail-dialog').open;
    try{
      const response=await fetch(selection.url,{cache:'no-store',signal:controller.signal,headers:{Accept:'application/json'}});if(!response.ok)throw new Error('HTTP '+response.status);const detail=await response.json();
      if(!isCurrent())return;if(!detail||detail.lane!==selection.lane||detail.node!==selection.node)throw new Error('详情与当前步骤不匹配');
      text('flow-detail-title',detail.title||selection.title);text('flow-detail-scope',[detail.scope_label,detail.task_id&&!str(detail.scope_label).includes(detail.task_id)?'任务 '+detail.task_id:''].filter(Boolean).join(' · ')||'该步骤的本轮记录');
      text('flow-detail-summary',detail.summary||'暂无本轮步骤记录。');text('flow-detail-model',flowModelLabel({...detail,actor:detail.actor||selection.actor},{evidence:true}));text('flow-detail-blocked','阻塞 / 等待原因：'+str(detail.blocked_reason));$('flow-detail-blocked').hidden=!detail.blocked_reason;
      text('flow-detail-status',(connected?'详情已同步 ':'状态连接中断 · 数据过期；详情读取于 ')+dateTime(Date.now())+' · 每 3 秒刷新');
      if(detail.artifact_notice)text('flow-detail-status',$('flow-detail-status').textContent+' · '+str(detail.artifact_notice));
      if(selection.lane==='art'&&selection.node==='review'&&detail.rework_history){acceptArtReworkDetail(detail);text('flow-detail-status',(connected?'详情已同步 ':'状态连接中断 · 数据过期；详情读取于 ')+dateTime(Date.now())+' · 返工历史每 15 秒刷新');}
      renderFlowEvidenceMenu(detail);
      if(flowDialog.evidence){const current=flowDialog.items.find(item=>item.url===flowDialog.evidence.url);if(current){flowDialog.evidence=current;if(!flowDialog.evidenceController)loadFlowEvidence(current,token);}else{flowDialog.evidenceController?.abort();flowDialog.evidenceSequence++;flowDialog.evidenceController=null;flowDialog.evidence=null;resetFlowEvidence('当前证据已不在本轮步骤清单中，请重新选择。');text('flow-evidence-title','选择日志或产物');}}
    }catch(error){if(isCurrent())text('flow-detail-status','详情数据过期，暂时无法同步；保留最近已读取内容。'+(error.name==='AbortError'?'请求超时。':str(error.message)));}
    finally{clearTimeout(timeout);if(sequence===flowDialog.detailSequence)flowDialog.detailController=null;}
  }
  function openFlowDetail(button,explicitItem=null){
    const repairEntry=button.id==='user-repair-detail',overviewNode=button.dataset.overviewNode;
    const lane=repairEntry||overviewNode?'development':button.dataset.flowLane,id=repairEntry?explicitItem?.id:overviewNode||button.dataset.flowNode;
    const item=explicitItem||data?.flows?.[lane]?.nodes?.find(value=>value.id===id);if(!item)return;
    const selection={lane,node:id,title:item.title,actor:item.actor},url=flowURL(item.detail_url,'detail',selection);if(!url){toast('当前步骤详情链接不可用。');return;}selection.url=url;
    flowDialog.token++;flowDialog.detailController?.abort();flowDialog.evidenceController?.abort();flowDialog.detailSequence++;flowDialog.evidenceSequence++;flowDialog.detailController=null;flowDialog.evidenceController=null;clearInterval(flowDialog.timer);
    flowDialog.selection=selection;flowDialog.trigger=button;flowDialog.evidence=null;flowDialog.items=[];flowDialog.menuFingerprint='';
    text('flow-detail-title',item.title||id);text('flow-detail-scope',flowLaneNames[lane]+' · 本轮步骤');text('flow-detail-summary',item.summary);text('flow-detail-model',flowModelLabel(item,{evidence:true}));text('flow-detail-blocked','阻塞 / 等待原因：'+str(item.blocked_reason));$('flow-detail-blocked').hidden=!item.blocked_reason;
    text('flow-detail-status','正在读取详情…');$('flow-detail-logs').replaceChildren();$('flow-detail-artifacts').replaceChildren();text('flow-evidence-title','选择日志或产物');resetFlowEvidence();
    $('flow-detail-reworks').hidden=!(lane==='art'&&id==='review');
    if(!$('flow-detail-dialog').open)$('flow-detail-dialog').showModal();loadFlowDetail();flowDialog.timer=setInterval(loadFlowDetail,lane==='art'&&id==='review'?15000:3000);
  }
  $('flows-lanes').addEventListener('click',event=>{const button=event.target.closest('button[data-flow-node]');if(button)openFlowDetail(button);});
  $('pipeline-milestone').addEventListener('click',event=>{const button=event.target.closest('button[data-flow-node]');if(button)openFlowDetail(button);});
  $('overview-steps').addEventListener('click',event=>{const button=event.target.closest('button[data-overview-node]');if(button)openFlowDetail(button);});
  $('flow-detail-close').addEventListener('click',()=>$('flow-detail-dialog').close());
  function userRepairDetailItem(snapshot){
    const repair=snapshot.task?.user_repair;if(!repair)return null;
    const current=snapshot.workflow?.current,id=['code','check','review'].includes(current)?current:'code';
    const item=snapshot.flows?.development?.nodes?.find(value=>value.id===id)||snapshot.workflow?.nodes?.find(value=>value.id===id)||{};
    return {...item,title:id==='code'?repair.title:(item.title||repair.title),actor:item.actor||(id==='code'?'Claude Code':id==='review'?'Codex':'UE / 控制器'),detail_url:'/api/flow/detail?lane=development&node='+id,id};
  }
  function renderUserRepair(snapshot){
    const repair=snapshot.task?.user_repair,error=snapshot.task?.user_repair_error;
    const accepted=snapshot.full_flow_test?.accepted_repair,panel=$('user-repair-panel'),detail=$('user-repair-detail');
    panel.hidden=!repair&&!error&&!accepted;
    if(!repair&&!error&&accepted){
      text('user-repair-title',prettyModel(accepted.model)+' 全流程修复');text('user-repair-status','验收通过');
      text('user-repair-summary',Number.isInteger(accepted.total_checks)?'独立检查 '+accepted.passed_checks+' / '+accepted.total_checks+' 项通过，已完成独立验收。':'独立全流程检查和任务审核均已通过。');
      text('user-repair-model',prettyModel(accepted.model)+' · '+str(accepted.effort));text('user-repair-stage','验收时间 · '+dateTime(accepted.accepted_at));
      text('user-repair-scope',str(accepted.task_id)+' · 验收版本 '+shortSHA(accepted.head_sha));
      detail.textContent='查看验收记录 →';detail.disabled=false;return;
    }
    detail.textContent='查看本批授权、日志与产物 →';
    if(!repair){if(error){text('user-repair-title','用户修复授权待核实');text('user-repair-status','记录待核实');text('user-repair-summary',error);text('user-repair-model','');text('user-repair-stage','');text('user-repair-scope','');detail.disabled=true;}return;}
    const s=snapshot.status||{},current=snapshot.workflow?.nodes?.find(value=>value.id===snapshot.workflow?.current);
    text('user-repair-title',repair.title);text('user-repair-status',!connected?'数据过期':current?.state_label||s.label||'等待执行');
    text('user-repair-summary','按本批用户授权修复全流程测试发现的问题；修改仍须经过独立检查和审核。'+(repair.plan_available?'本批计划可在产物中查看。':'本批计划尚未形成可核实产物。')+' '+(snapshot.task?.user_followup?.summary||snapshot.task?.user_followup_error||''));
    text('user-repair-model','本批 CODE 授权 · '+prettyModel(repair.model)+' · '+str(repair.effort));text('user-repair-stage','当前阶段 · '+(current?.title||phaseNames[s.phase]||'等待同步'));
    text('user-repair-scope',str(snapshot.task.id)+' · 依据版本 '+shortSHA(repair.source_head)+(Number.isInteger(repair.activation_rework)?' · 本批从累计第 '+repair.activation_rework+' 次开发起':'')+'；此前返工记录保留。');
    detail.disabled=false;
  }
  $('user-repair-detail').addEventListener('click',event=>{
    const item=data&&userRepairDetailItem(data);
    if(item){openFlowDetail(event.currentTarget,item);return;}
    const accepted=data?.full_flow_test?.accepted_repair;
    if(!accepted)return;
    location.hash='history';routeView();
    const review=[...$('accepted').querySelectorAll('details')].find(el=>el.dataset.taskId===accepted.task_id);
    if(review){
      if(review.closest('tr')?.hidden){$('history-search').value='';filterHistory();}
      review.open=true;review.scrollIntoView({block:'center'});review.querySelector('summary').focus({preventScroll:true});
    }
  });
  function renderFullFlowTest(snapshot){
    const item=snapshot.full_flow_test,error=snapshot.full_flow_error;$('full-flow-test').hidden=!item&&!error;
    if(!item){$('full-flow-retest').hidden=true;$('full-flow-package-short').hidden=true;if(error){text('full-flow-status','记录待核实');text('full-flow-summary',error);for(const id of ['full-flow-model','full-flow-stage','full-flow-updated','full-flow-scope'])text(id,'—');$('full-flow-detail').disabled=true;}return;}
    const repairBasis=snapshot.task?.user_repair?.source_full_flow_run===item.id;
    const historical=!!(item.historical||repairBasis||item.accepted_repair);
    $('full-flow-test').dataset.historical=String(historical);text('full-flow-title',repairBasis?'本批修复的历史测试依据':item.accepted_repair?'修复前的全流程测试':historical?'历史全流程测试':item.title||'全流程测试');text('full-flow-status',item.accepted_repair?'修复前记录':item.status_label||item.status);text('full-flow-summary',item.summary||'本次测试按固定版本独立执行，主任务状态另行显示。');
    text('full-flow-model','测试执行者 · '+(item.actor||'未记录')+' · '+flowModelLabel(item));text('full-flow-stage','阶段 · '+(item.stage_label||item.stage));text('full-flow-updated','更新 · '+dateTime(item.stage_updated_at||item.updated_at));
    text('full-flow-scope',(repairBasis?'本批修复的历史依据 · ':item.historical?'历史测试版本 · ':'固定测试版本 · ')+str(item.task_id)+' · HEAD '+shortSHA(item.head_sha)+(item.historical&&item.current_head_sha?' · 当前工作区 '+shortSHA(item.current_head_sha):'')+'。独立测试不改变主任务状态或验收结论。');
    $('full-flow-package-short').hidden=!item.package_short;if(item.package_short)text('full-flow-package-short',item.package_short.summary);
    $('full-flow-retest').hidden=!item.retest;if(item.retest)text('full-flow-retest',[item.retest.summary||'补充复测记录单独列示，首轮结果保留。',item.retest.reconciliation_summary].filter(Boolean).join(' '));
    $('full-flow-detail').disabled=false;
  }
  $('full-flow-detail').addEventListener('click',event=>{if(data?.full_flow_test)openFlowDetail(event.currentTarget,data.full_flow_test);});
  $('flow-detail-dialog').addEventListener('close',()=>{
    const selection=flowDialog.selection;flowDialog.token++;flowDialog.detailSequence++;flowDialog.evidenceSequence++;flowDialog.detailController?.abort();flowDialog.evidenceController?.abort();flowDialog.detailController=null;flowDialog.evidenceController=null;clearInterval(flowDialog.timer);releaseFlowImage();
    const target=flowDialog.trigger?.id==='user-repair-detail'?flowDialog.trigger:selection&&findFlowButton(selection.lane,selection.node)||flowDialog.trigger;flowDialog.selection=null;if(target?.isConnected)target.focus({preventScroll:true});
  });

  const artReworks={taskId:'',contextKey:'',requestedKey:'',requestedAt:0,receivedAt:0,controller:null,sequence:0,fingerprint:'',detail:null,roundCards:new Map(),imageTrigger:null,imageURL:''};
  const reworkSelection={lane:'art',node:'review'};
  const reworkDecision=value=>({rework:'技术审核退回返工',blocked:'历史阻塞',approved:'技术审核通过',accepted:'技术审核通过',in_progress:'本轮进行中',completed:'已完成'}[value]||str(value)||'审核结果未记录');
  const userReworkDecision=value=>({accepted:'用户接受',rework:'用户退回返工'}[value]||str(value)||'');
  function reworkEvidenceURL(value){const safe=flowURL(value,'evidence',reworkSelection);if(!safe)return null;const url=new URL(safe);return url.pathname+url.search;}
  function reworkRoundKey(round){return str(round.index??round.label);}
  function briefReworkReason(round){
    if(!round)return '当前制作中，尚无返工记录。';
    let value=str(round.summary||round.feedback||'请展开查看审核记录。').replace(/\s+/g,' ').trim();
    if(value.includes('；但'))value=value.slice(value.indexOf('；但')+2);
    value=value.replace(/本结论不代表用户视觉批准[。.]?/g,'').trim();
    return value.length>160?value.slice(0,159)+'…':value;
  }
  function buildReworkImage(shot,title,key,caption){
    const url=reworkEvidenceURL(shot.url);if(!url)return null;
    const button=node('button','art-rework-image-button');button.type='button';button.dataset.reworkImage=key;button.dataset.evidenceUrl=url;button.setAttribute('aria-label','放大'+title+' · '+str(caption||shot.title||'截图'));
    const picture=node('img');picture.src=url;picture.alt=title+' · '+str(caption||shot.title||'截图');picture.loading='lazy';const label=node('span','',caption||shot.title||'截图');
    picture.addEventListener('error',()=>{picture.hidden=true;label.textContent=str(caption||shot.title||'截图')+' · 暂不可读取';});
    button.append(picture,label);button.addEventListener('click',()=>openReworkImage(button,shot,title,url));return button;
  }
  function renderLatestReworkImages(round){
    const images=(Array.isArray(round?.evidence_images)?round.evidence_images:[]).filter(shot=>reworkEvidenceURL(shot.url));
    const first=images.find(shot=>/Blender.*L8/i.test(shot.title||''))||images.find(shot=>/Blender/i.test(shot.title||''))||images[0];
    const second=images.find(shot=>shot!==first&&/07_L8_details/i.test(shot.title||''))||images.find(shot=>shot!==first);
    const selected=[first,second].filter(Boolean),container=$('art-rework-latest-images');
    container.hidden=!selected.length;
    container.replaceChildren(...selected.map(shot=>{
      const caption=/Blender.*L8/i.test(shot.title||'')?'高级住宅屋顶':/Blender.*L4/i.test(shot.title||'')?'中级住宅屋顶':/07_L8_details/i.test(shot.title||'')?'高级住宅近景':'审核截图';
      return buildReworkImage(shot,round.label||'最近审核',reworkRoundKey(round),caption);
    }).filter(Boolean));
  }
  function buildReworkRound(round,previous,isLatest){
    const key=reworkRoundKey(round),row=node('details','art-rework-round');row.dataset.reworkRound=key;row.open=previous?previous.open:isLatest;
    const title=str(round.label||('记录 '+key)),summary=node('summary');summary.append(node('strong','',title),node('span','tag',reworkDecision(round.decision)));row.append(summary);
    const body=node('div','art-rework-round-body'),meta=node('div','art-rework-meta'),reviewSource=node('span','',round.review_source||'Codex 技术审核（本轮未记录）');reviewSource.dataset.reviewSource=str(round.review_source||'');
    meta.append(reviewSource,node('span','',round.reviewed_at?'记录时间 '+dateTime(round.reviewed_at,true):'审核时间未记录'),node('span','',round.time_source?'时间来源：'+round.time_source:'时间来源未记录'));body.append(meta);
    if(round.production_mode)body.append(node('p','small','本轮执行方式：'+({recipe_only:'旧权限 · 仅编辑配方，不代表完成模型修改',claude_blender_job:'Claude 通过 Blender 制作任务执行'}[round.production_mode]||str(round.production_mode))));
    if(round.summary)body.append(node('p','art-rework-text',round.summary));
    for(const section of Array.isArray(round.sections)?round.sections:[]){
      const item=node('section','art-rework-section');item.dataset.reworkSection=section.kind||'other';
      const category={completed:'已修好部分 · 审核原文',required:'仍需修复 · 审核原文',limitations:'证据限制 · 审核原文'}[section.kind];
      item.append(node('h4','',category?category+(section.title?' / '+str(section.title):''):section.title||'审核原文'),node('p','art-rework-text',section.text));body.append(item);
    }
    if(round.feedback){const raw=node('section','art-rework-section');raw.dataset.reworkRaw=key;const toggle=node('button','art-rework-refresh','原始审核全文与原因'),content=node('div','art-rework-text',round.feedback);toggle.type='button';content.dataset.reworkRawText=key;content.hidden=previous?.querySelector('[data-rework-raw-text]')?.hidden!==false;toggle.setAttribute('aria-expanded',String(!content.hidden));toggle.addEventListener('click',()=>{content.hidden=!content.hidden;toggle.setAttribute('aria-expanded',String(!content.hidden));});raw.append(toggle,content);body.append(raw);}
    const delivery=node('section','art-rework-section'),deliverySource=node('h4','',round.delivery_source||'Claude 交付声明');deliverySource.dataset.deliverySource=str(round.delivery_source||'Claude交付声明');
    const deliveryText=node('p','art-rework-text',round.delivery_text||'本轮尚无可读取的 Claude 实际交付说明。');deliveryText.dataset.deliveryText=key;delivery.append(deliverySource,node('p','art-rework-source','以下为 Claude 的交付声明或自行提出的制作方案，独立于用户意见和审核结论。'),deliveryText);body.append(delivery);
    const note=node('p','art-rework-evidence-note',round.evidence_note||'仅展示本轮留存的可读取证据；历史截图不代表当前验收包。');note.dataset.evidenceNote=round.evidence_status||'unavailable';body.append(note);
    if(Array.isArray(round.checks)&&round.checks.length){
      const checks=node('div','art-rework-checks');checks.setAttribute('aria-label','该审核轮次的检查记录');
      for(const check of round.checks){const label=check.passed===true?'通过':check.passed===false?'未通过':'未记录结果';const item=node('span','tag',str(check.id)+' · '+label+(check.head_sha?' · '+shortSHA(check.head_sha):''));item.title=check.head_sha||'';checks.append(item);}body.append(node('p','art-rework-source','检查对应范围：'+str(round.checks_status||'未记录')),checks);
    }else body.append(node('p','art-rework-source','本轮没有可确认对应的独立检查回执。'));
    const images=node('div','art-rework-images');
    for(const shot of Array.isArray(round.evidence_images)?round.evidence_images:[]){const button=buildReworkImage(shot,title,key);if(button)images.append(button);}
    if(images.children.length)body.append(node('p','art-rework-source','以下为 '+title+' 的历史留存截图，点击放大；不作为当前版本的接受入口。'),images);
    const files=node('ul','art-rework-files'),seen=new Set();
    for(const file of [...(Array.isArray(round.logs)?round.logs:[]),...(Array.isArray(round.artifacts)?round.artifacts:[])]){
      const url=reworkEvidenceURL(file.url);if(!url||seen.has(url))continue;seen.add(url);const li=node('li'),link=node('a','',file.title||file.id||'原回执文件');link.href=url;link.target='_blank';link.rel='noopener noreferrer';link.dataset.reworkArtifact=key;link.append(node('span','',evidenceSize(file.size_bytes)?' · '+evidenceSize(file.size_bytes):''));li.append(link);files.append(li);
    }
    if(files.children.length)body.append(node('h4','art-rework-source','原始回执与对应文件'),files);row.append(body);return row;
  }
  function acceptArtReworkDetail(detail){
    const history=detail?.rework_history;if(!history||typeof history!=='object')return;
    const expected=str(data?.art?.task_id);if(expected&&detail.task_id&&detail.task_id!==expected)return;
    if(artReworks.taskId&&detail.task_id&&artReworks.taskId!==detail.task_id){artReworks.roundCards.clear();artReworks.fingerprint='';}
    artReworks.taskId=str(detail.task_id||expected);artReworks.detail=detail;artReworks.receivedAt=Date.now();artReworks.requestedAt=Date.now();artReworks.requestedKey=artReworks.contextKey;
    const rounds=(Array.isArray(history.rounds)?history.rounds:[]).slice().sort((a,b)=>(number(b.index)??-1)-(number(a.index)??-1));
    const completed=rounds.filter(round=>round.decision!=='in_progress'),latest=completed[0],latestReturned=completed.find(round=>!['approved','accepted','completed'].includes(round.decision))||latest;
    const count=history.summary||{};text('art-rework-summary',count.current_job_round?'第 '+count.current_job_round+' 轮制作中':count.phase==='CODE'?'正在制作':'当前进度见制作流程');
    text('art-rework-count-detail',count.text||('已留存 '+completed.length+' 次完成的审核记录。'));
    text('art-rework-latest-title',latestReturned?(['accepted','approved'].includes(latestReturned.decision)?'最近审核结果':'上次退回原因'):'返工原因');
    text('art-rework-latest-summary',briefReworkReason(latestReturned));
    text('art-rework-status',(connected?'更新于 ':'连接中断，保留上次记录 ')+dateTime(artReworks.receivedAt));
    const fingerprint=JSON.stringify(history);if(artReworks.fingerprint===fingerprint)return;artReworks.fingerprint=fingerprint;
    const scrollY=window.scrollY,focused=document.activeElement,focusedRound=focused?.closest('[data-rework-round]')?.dataset.reworkRound,focusedImage=focused?.dataset.evidenceUrl;
    renderLatestReworkImages(latestReturned);
    const nextCards=new Map(),elements=rounds.map(round=>{const key=reworkRoundKey(round),old=artReworks.roundCards.get(key),value=JSON.stringify(round),element=old?.fingerprint===value?old.element:buildReworkRound(round,old?.element,round===latest);nextCards.set(key,{element,fingerprint:value});return element;});
    artReworks.roundCards=nextCards;
    if(!elements.length)$('art-rework-rounds').replaceChildren(node('p','empty','暂无可读取的历史审核轮次。'));
    else if(elements.some((element,index)=>$('art-rework-rounds').children[index]!==element)||$('art-rework-rounds').children.length!==elements.length)$('art-rework-rounds').replaceChildren(...elements);
    const user=node('h3','','用户原始验收意见'),feedbacks=Array.isArray(history.user_feedback)?history.user_feedback:[];
    $('art-rework-user-feedback').replaceChildren(user,...(feedbacks.length?feedbacks.map(feedback=>{const row=node('article','art-rework-feedback');row.append(node('p','art-rework-source',(feedback.source||'用户')+' · 用户验收意见'+(feedback.recorded_at?' · '+dateTime(feedback.recorded_at,true):' · 时间未记录')+(feedback.decision?' · '+userReworkDecision(feedback.decision):'')),node('p','art-rework-text',feedback.feedback||'该条记录未提供意见正文。'));if(feedback.review_package_sha256)row.append(node('p','art-rework-source hash','对应包 SHA256：'+str(feedback.review_package_sha256)));return row;}):[node('p','empty','没有可读取的用户原始验收意见。Claude 自选方案不会在这里冒充用户要求。')]));
    if(focusedRound&&focused&&!focused.isConnected){const row=nextCards.get(focusedRound)?.element;const target=focusedImage&&[...(row?.querySelectorAll('[data-rework-image]')||[])].find(button=>button.dataset.evidenceUrl===focusedImage)||row?.querySelector('summary');target?.focus({preventScroll:true});}
    if(activeView==='art')window.scrollTo(0,scrollY);
  }
  function syncArtReworkContext(snapshot){
    const art=snapshot.art||{},taskId=str(art.task_id),key=JSON.stringify([taskId,art.reworks,art.phase,art.status]);
    if(taskId&&artReworks.taskId&&taskId!==artReworks.taskId){artReworks.sequence++;artReworks.controller?.abort();artReworks.controller=null;artReworks.detail=null;artReworks.roundCards.clear();artReworks.fingerprint='';artReworks.receivedAt=0;artReworks.taskId=taskId;artReworks.requestedKey='';$('art-rework-rounds').replaceChildren();$('art-rework-latest-images').replaceChildren();$('art-rework-latest-images').hidden=true;text('art-rework-latest-summary','正在读取新任务的记录…');}
    artReworks.contextKey=key;
    if(!artReworks.receivedAt&&art.blocking_summary)text('art-rework-latest-summary',art.blocking_summary);
    maybeRefreshArtReworks();
  }
  async function maybeRefreshArtReworks(force=false){
    if(activeView!=='art'||document.hidden||!data||artReworks.controller)return;
    const art=data.art||{};if(art.configured===false&&!art.task_id){text('art-rework-status','美术执行器尚未配置，暂无审核历史。');return;}
    if(!force&&artReworks.requestedKey===artReworks.contextKey&&Date.now()-artReworks.requestedAt<15000)return;
    const controller=new AbortController(),sequence=++artReworks.sequence,key=artReworks.contextKey;artReworks.controller=controller;artReworks.requestedAt=Date.now();artReworks.requestedKey=key;
    const timeout=setTimeout(()=>controller.abort(),7000);$('art-rework-refresh').disabled=true;text('art-rework-status',artReworks.receivedAt?'正在同步返工记录，保留已展开的历史证据…':'正在读取返工记录与证据…');
    try{
      const response=await fetch('/api/flow/detail?lane=art&node=review',{cache:'no-store',signal:controller.signal,headers:{Accept:'application/json'}});if(!response.ok){let failure;try{failure=await response.json();}catch{}throw new Error(failure?.error||'HTTP '+response.status);}const detail=await response.json();
      if(sequence!==artReworks.sequence||key!==artReworks.contextKey)return;if(detail?.lane!=='art'||detail?.node!=='review')throw new Error('返回记录与美术审核不匹配');
      if(!detail.rework_history){text('art-rework-status','当前服务尚无可读取的逐轮历史；已有退回摘要仍保留。');return;}acceptArtReworkDetail(detail);
    }catch(error){if(sequence===artReworks.sequence)text('art-rework-status','返工历史数据过期，暂时无法读取。保留已显示的历史记录，历史图片不代表当前验收包。'+(error.name==='AbortError'?'请求超时。':str(error.message)));}
    finally{clearTimeout(timeout);if(sequence===artReworks.sequence){artReworks.controller=null;$('art-rework-refresh').disabled=false;}}
  }
  function openReworkImage(button,shot,roundTitle,url){
    artReworks.imageTrigger=button;artReworks.imageURL=url;text('art-rework-image-title',roundTitle+' · '+str(shot.title||shot.id||'历史截图'));text('art-rework-image-note','这是 '+roundTitle+' 的历史留存证据，不是当前验收包。');
    $('art-rework-image').hidden=false;$('art-rework-image').alt=roundTitle+' · '+str(shot.title||shot.id||'历史截图');$('art-rework-image').src=url;$('art-rework-image-original').href=url;if(!$('art-rework-image-dialog').open)$('art-rework-image-dialog').showModal();
  }
  $('art-rework-image').addEventListener('error',()=>{$('art-rework-image').hidden=true;text('art-rework-image-note','这张历史截图暂不可读取，证据可能已更新或不可用。请刷新返工记录后重试。');text('art-rework-status','历史截图不可读取，不能据此确认当前验收包。请刷新记录后重试。');});
  $('art-rework-image-close').addEventListener('click',()=>$('art-rework-image-dialog').close());
  $('art-rework-image-dialog').addEventListener('close',()=>{const target=artReworks.imageTrigger?.isConnected?artReworks.imageTrigger:[...$('art-rework-panel').querySelectorAll('[data-rework-image]')].find(button=>button.dataset.evidenceUrl===artReworks.imageURL);$('art-rework-image').removeAttribute('src');if(target?.isConnected)target.focus({preventScroll:true});});
  $('art-rework-refresh').addEventListener('click',()=>maybeRefreshArtReworks(true));
  $('refresh').addEventListener('click',()=>maybeRefreshArtReworks(true));
  window.addEventListener('hashchange',()=>maybeRefreshArtReworks());
  $('flow-detail-rework-open').addEventListener('click',()=>{$('flow-detail-dialog').close();location.hash='art';requestAnimationFrame(()=>{$('art-rework-panel').scrollIntoView({block:'start'});maybeRefreshArtReworks();});});
  setInterval(()=>maybeRefreshArtReworks(),15000);

  let artDiagnosisKey='';
  const artDiagnosisDrafts=new Map(),artDiagnosisMessages=new Map(),artDiagnosisPending=new Set(),artDiagnosisSubmitted=new Set();
  function diagnosisResponseKey(item){return item?str(item.task_id)+':'+str(item.receipt_sha256):'';}
  function syncArtDiagnosisActions(){
    const item=data?.art?.diagnosis_response,key=diagnosisResponseKey(item),valid=!!item&&data?.art?.phase==='BLOCKED'&&item.status==='waiting_user'&&item.can_respond===true;
    $('art-diagnosis-send').disabled=!valid||!connected||artDiagnosisPending.has(key)||artDiagnosisSubmitted.has(key);
    $('art-diagnosis-feedback').disabled=artDiagnosisPending.has(key)||artDiagnosisSubmitted.has(key)||!!item&&item.can_respond===false;
    text('art-diagnosis-message',artDiagnosisMessages.get(key)||(!connected?'连接中断，恢复同步后可发送回复。':item?.can_respond===false?'回复已排队，等待重新诊断。':''));
  }
  function renderArtDiagnosis(art){
    const item=art.diagnosis_response,key=diagnosisResponseKey(item),visible=!!item&&art.phase==='BLOCKED'&&item.status==='waiting_user';
    $('art-diagnosis-response').hidden=!visible;
    if(!visible)return;
    if(key!==artDiagnosisKey){if(artDiagnosisKey)artDiagnosisDrafts.set(artDiagnosisKey,$('art-diagnosis-feedback').value);artDiagnosisKey=key;$('art-diagnosis-feedback').value=artDiagnosisDrafts.get(key)||'';}
    text('art-diagnosis-reason',item.reason||'该诊断需要你的决定或补充条件。');text('art-diagnosis-summary',item.summary);text('art-diagnosis-plan',item.plan||'暂无可读取的计划摘要。');
    renderList('art-diagnosis-questions',Array.isArray(item.questions)?item.questions:[],question=>node('li','',question));syncArtDiagnosisActions();
  }
  $('art-diagnosis-feedback').addEventListener('input',()=>{if(artDiagnosisKey)artDiagnosisDrafts.set(artDiagnosisKey,$('art-diagnosis-feedback').value);});
  $('art-diagnosis-send').addEventListener('click',async()=>{
    const item=data?.art?.diagnosis_response,key=diagnosisResponseKey(item),feedback=$('art-diagnosis-feedback').value;
    if(!item||data?.art?.phase!=='BLOCKED'||item.status!=='waiting_user'||!item.can_respond||key!==artDiagnosisKey||artDiagnosisPending.has(key)||artDiagnosisSubmitted.has(key))return;
    if(!connected){artDiagnosisMessages.set(key,'连接中断，请刷新核对诊断版本后发送。');syncArtDiagnosisActions();return;}
    if(!feedback.trim()){artDiagnosisMessages.set(key,'请先填写你的决定或补充条件。');syncArtDiagnosisActions();$('art-diagnosis-feedback').focus();return;}
    artDiagnosisPending.add(key);artDiagnosisMessages.set(key,'正在提交本次诊断的回复…');syncArtDiagnosisActions();
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),15000);
    try{
      const response=await fetch('/api/art/diagnosis/respond',{method:'POST',signal:controller.signal,headers:{'Content-Type':'application/json','X-Art-Action-Token':data?.art?.action_token||''},body:JSON.stringify({task_id:item.task_id,receipt_sha256:item.receipt_sha256,feedback})});const result=await response.json();if(!response.ok)throw new Error(result.error||'回复未提交，请刷新核对诊断版本。');
      artDiagnosisSubmitted.add(key);artDiagnosisMessages.set(key,'回复已提交，等待重新诊断。');toast('美术诊断回复已提交');await refresh();
    }catch(error){artDiagnosisMessages.set(key,error.name==='AbortError'?'提交响应超时，请刷新核对是否已收到，再决定是否重发。':str(error.message));await refresh();}
    finally{clearTimeout(timeout);artDiagnosisPending.delete(key);syncArtDiagnosisActions();}
  });

  const artCards=new Map(),artDrafts=new Map(),artPending=new Set(),artSubmitted=new Set(),artMessages=new Map();
  const artKey=item=>str(item.id)+':'+str(item.preview_sha256||item.review_package_sha256)+':'+(item.preview_kind||'review');
  const artLabels={queued:'已排队',active:'制作中',awaiting_approval:'等待视觉验收',accepted:'已接受',rework:'待返工',failed:'需要处理'};
  const manualArtReview=item=>item.status==='awaiting_approval'&&!item.decision&&item.preview_kind!=='technical';
  const shortArtReviewReason=value=>{const reason=str(value).replace(/\s+/g,' ');return reason.length>140?reason.slice(0,139)+'…':reason;};
  const artReviewReason=item=>shortArtReviewReason(item.preview_error||item.accept_block_reason||'当前版本尚未具备人工验收条件，请等待状态更新。');
  function renderArtApprovalEntry(requests){
    const pending=requests.filter(manualArtReview);$('art-manual-entry').hidden=!pending.length;
    renderList('art-manual-entry-list',pending.map(item=>({id:item.id,title:item.title,version:artKey(item),ready:!!item.can_resolve,can_accept:item.can_accept,reason:item.can_resolve?(item.can_accept===false?shortArtReviewReason(item.accept_block_reason||'接受前仍需核对验收条件。'):''):artReviewReason(item)})),item=>{
      const row=node('div','art-manual-entry-row'),copy=node('div');row.dataset.artReviewEntry=str(item.id);
      copy.append(node('strong','',item.title||item.id),node('p','',item.ready?(item.can_accept===false?'可填写意见与退回；接受暂不可用：'+item.reason:'待你填写意见并人工确认当前版本。'):'暂不可确认：'+item.reason));
      const button=node('button','',item.ready?'前往人工验收':'查看验收入口与原因');button.type='button';button.addEventListener('click',()=>{const target=[...$('art-queue').querySelectorAll('[data-art-manual-review]')].find(element=>element.dataset.artManualReview===str(item.id));if(target){target.scrollIntoView({block:'start'});target.querySelector('.manual-review-heading')?.focus({preventScroll:true});}});
      row.append(copy,button);return row;
    });
  }
  function artLatest(item){return (data?.art?.requests||[]).find(current=>artKey(current)===artKey(item));}
  function syncArtActions(){
    for(const entry of artCards.values()){
      const latest=artLatest(entry.item),key=artKey(entry.item);
      for(const button of entry.card.querySelectorAll('.art-actions button'))button.disabled=!connected||!latest?.can_resolve||!manualArtReview(latest)||artPending.has(key)||artSubmitted.has(key)||(button.classList.contains('accept')&&latest?.can_accept===false);
      const message=entry.card.querySelector('[data-art-message]');
      if(message)message.textContent=artMessages.get(key)||(!connected?'连接已中断，恢复同步后可提交验收意见。':'');
    }
  }
  async function submitArt(item,decision,feedback){
    const key=artKey(item),current=artLatest(item);
    if(artPending.has(key)||artSubmitted.has(key))return;
    if(!connected){artMessages.set(key,'连接已中断，请刷新状态后再验收。');syncArtActions();return;}
    if(!current?.can_resolve||!manualArtReview(current)){artMessages.set(key,'版本或验收状态已更新，请重新查看当前版本。');syncArtActions();return;}
    if(decision==='accepted'&&current.can_accept===false){artMessages.set(key,current.accept_block_reason||'当前费用记录待核实，暂时无法接受。');syncArtActions();return;}
    if(!feedback.value.trim()){artMessages.set(key,'请填写本版的验收意见。');syncArtActions();feedback.focus();return;}
    artPending.add(key);artMessages.set(key,'正在核验资产并提交，请稍候…');syncArtActions();
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),180000);
    try{
      const response=await fetch('/api/art/resolve',{method:'POST',signal:controller.signal,headers:{'Content-Type':'application/json','X-Art-Action-Token':data?.art?.action_token||''},body:JSON.stringify({task_id:item.id,decision,feedback:feedback.value.trim(),review_package_sha256:item.review_package_sha256})});
      const result=await response.json();if(!response.ok)throw new Error(result.error||'提交失败');
      artSubmitted.add(key);
      artMessages.set(key,decision==='accepted'?'本版已接受，系统将登记结果并自动创建入库任务。':'返工意见已提交，将沿用本任务继续修改。');
      toast('美术验收意见已提交');await refresh();
    }catch(error){
      artMessages.set(key,error.name==='AbortError'?'提交响应超时，请刷新核对本版状态后再操作。':str(error.message));
      await refresh();
    }finally{clearTimeout(timeout);artPending.delete(key);syncArtActions();}
  }
  function previewName(name){
    const known={all_levels:'三级住宅总览',l1:'低级住宅',l4:'中级住宅',l8:'高级住宅',l1_courtyard:'低级 · 院落',l4_courtyard:'中级 · 院落',l8_courtyard:'高级 · 院落',l1_details:'低级 · 细节',l4_details:'中级 · 细节',l8_details:'高级 · 细节',rear_structure:'背面结构',props:'生活道具',scale:'人物尺度',closeup:'材质近景',overview:'整体效果'};
    const value=str(name).replace(/\.(png|jpg|jpeg)$/i,'').replace(/^\d+[_ -]*/, '');
    return known[value.toLowerCase()]||value.replaceAll('_',' ');
  }
  function buildArtCard(item,previous){
    const key=artKey(item),card=node('article','art-batch'),head=node('div','art-batch-head'),title=node('div');
    card.dataset.artKey=key;card.dataset.status=item.status||'';
    title.append(node('div','small hash',item.id),node('h3','',item.title||item.id));
    title.append(node('div','art-batch-meta',[item.batch_id,item.approved_levels?.join(' / ')].filter(Boolean).join(' · ')));
    head.append(title,node('span','tag',item.preview_kind==='technical'?'制作过程预览':item.decision?'验收意见已提交':artLabels[item.status]||item.status));card.append(head);
    const workbench=node('div','art-workbench'),visuals=node('div','art-visuals'),side=node('div','art-review-side');
    const shots=(item.previews||[]).filter(shot=>typeof shot.url==='string'&&shot.url.startsWith('/api/art/preview?'));
    if(shots.length){
      const hero=node('a','art-gallery-main');hero.target='_blank';hero.rel='noopener noreferrer';hero.title='打开完整截图';
      const picture=node('img'),caption=node('div','art-gallery-caption'),captionName=node('span');caption.append(captionName,node('span','','查看原图 ↗'));hero.append(picture,caption);
      const gallery=node('div','art-previews');gallery.setAttribute('aria-label','选择预览视角');
      const select=shot=>{hero.href=shot.url;picture.src=shot.url;picture.alt=previewName(shot.name);captionName.textContent=previewName(shot.name);card.dataset.selectedPreview=shot.url;for(const button of gallery.children)button.setAttribute('aria-pressed',String(button.dataset.url===shot.url));};
      for(const shot of shots){
        const button=node('button');button.type='button';button.dataset.url=shot.url;button.title=previewName(shot.name);button.setAttribute('aria-label','查看'+previewName(shot.name));
        const image=node('img');image.src=shot.url;image.alt=previewName(shot.name);image.loading='lazy';
        button.append(image,node('span','',previewName(shot.name)));button.addEventListener('click',()=>select(shot));gallery.append(button);
      }
      select(shots.find(shot=>shot.url===previous?.dataset.selectedPreview)||shots[0]);
      visuals.append(hero,gallery);
    }else visuals.append(node('div','empty','此批次尚无可展示预览。生成并完成检查后会自动更新。'));
    if(item.preview_error)visuals.append(node('div','art-message bad',item.preview_error));
    const budget=node('div','art-budget');budget.append(node('div','review-section-title','本批累计费用'));
    if(item.budget){
      const b=item.budget,row=node('div','budget-row');row.append(node('strong','',b.actual.toLocaleString('zh-CN')),node('span','','/ '+b.limit.toLocaleString('zh-CN')+' 积分上限'));budget.append(row);
      const meter=node('progress','budget-meter');meter.max=b.limit||1;meter.value=b.committed;meter.setAttribute('aria-label','已用和已占用的积分');budget.append(meter,node('div','budget-note','剩余 '+b.remaining.toLocaleString('zh-CN')+' 积分'+(b.reserved||b.unknown?' · 另占用 '+(b.reserved+b.unknown)+' 积分':'')));
    }else budget.append(node('div','budget-note',item.budget_error||'预算待核实，当前没有可验证的费用记录。'));
    side.append(budget);
    if(item.technical_review){
      const review=node('details','technical-review');review.open=!!previous?.querySelector('.technical-review')?.open;
      review.append(node('summary','',item.technical_review.decision==='accept'?'技术检查通过 · 查看摘要':item.technical_review.decision==='accepted'?'技术检查通过 · 查看摘要':'技术检查 · 查看摘要'));
      review.append(node('p','',item.technical_review.summary||item.technical_review.feedback||'暂无摘要。'),node('p','small','技术检查不代替你的视觉验收。'));side.append(review);
    }
    const form=node('div','art-review-form');
    if(manualArtReview(item)){form.dataset.artManualReview=str(item.id);const heading=node('h3','manual-review-heading','人工验收 · 当前版本');heading.tabIndex=-1;form.append(heading,node('p','manual-review-note','你可在意见中说明仅作为当前阶段示意，保留后续调整要求。接受仍绑定本版证据，并会进入资产入库队列。'));}
    const displayVersion=item.preview_sha256||item.review_package_sha256;
    if(displayVersion){
      const version=node('div','art-proof-version');
      const sha=node('code','hash',shortSHA(displayVersion));
      version.append(node('span','',item.preview_kind==='technical'?'当前制作版本':'验收版本'),sha);
      sha.title=displayVersion;form.append(version);
    }
    if(item.preview_kind==='technical')form.append(node('div','art-technical-note','这是当前制作版本的技术证据，仅供查看。技术审核尚未通过，修复并重新检查后才能提交视觉验收。'));
    if(item.decision)form.append(node('div','art-message',(item.decision.decision==='accepted'?'已提交接受':'已提交返工')+' · '+item.decision.feedback));
    if(manualArtReview(item)){
      const label=node('label','small','你的人工验收意见'),feedback=node('textarea','art-feedback');feedback.maxLength=4000;feedback.placeholder='填写当前用途、认可范围，以及后续仍需调整的部分。';feedback.value=artDrafts.get(key)||'';
      feedback.addEventListener('input',()=>artDrafts.set(key,feedback.value));label.append(feedback);form.append(label);
      const actions=node('div','art-actions'),message=node('div','art-message');message.dataset.artMessage='';message.setAttribute('role','status');
      const accept=node('button','accept','接受本版'),rework=node('button','','退回修改');accept.type=rework.type='button';
      if(!item.can_resolve){accept.title=rework.title=artReviewReason(item);}
      accept.addEventListener('click',()=>submitArt(item,'accepted',feedback));rework.addEventListener('click',()=>submitArt(item,'rework',feedback));
      actions.append(accept,rework,node('span','small','意见绑定当前版本 · 接受后自动进入资产入库队列'));form.append(actions,message);
      if(!item.can_resolve)form.append(node('div','art-manual-unavailable','人工验收暂不可提交：'+artReviewReason(item)));
      else if(item.can_accept===false)form.append(node('div','art-accept-block',item.accept_block_reason||'当前费用记录待核实，暂时无法接受；可提交返工意见。'));
    }
    side.prepend(form);
    workbench.append(visuals,side);card.append(workbench);return card;
  }
  function renderArtCards(requests){
    const keys=new Set(requests.map(artKey));
    for(const [key,entry] of artCards){if(!keys.has(key)){entry.card.remove();artCards.delete(key);artSubmitted.delete(key);artDrafts.delete(key);artMessages.delete(key);}}
    requests.forEach((item,index)=>{
      const key=artKey(item),fingerprint=JSON.stringify(item),previous=artCards.get(key);
      if(item.decision||item.status!=='awaiting_approval'){
        artSubmitted.delete(key);artDrafts.delete(key);artMessages.delete(key);
      }
      let card=previous?.card;
      if(!previous||previous.fingerprint!==fingerprint){
        const field=card?.querySelector('.art-feedback'),focused=field===document.activeElement;
        const selection=focused?[field.selectionStart,field.selectionEnd,field.selectionDirection]:null;
        if(field&&!item.decision&&item.status==='awaiting_approval')artDrafts.set(key,field.value);
        const next=buildArtCard(item,card);
        if(card)card.replaceWith(next);card=next;
        artCards.set(key,{item,fingerprint,card});
        if(focused){const replacement=card.querySelector('.art-feedback');if(replacement){replacement.focus({preventScroll:true});replacement.setSelectionRange(...selection);}}
      }
      if($('art-queue').children[index]!==card)$('art-queue').insertBefore(card,$('art-queue').children[index]||null);
    });
    syncArtActions();
  }
  function overviewPreviewState(state){
    const ready=state==='ready';
    $('spotlight-image').hidden=!ready;
    $('spotlight-source').hidden=!ready;
    $('spotlight-empty').hidden=ready;
    text('spotlight-empty',state==='loading'?'正在载入批次预览':state==='failed'?'批次预览暂不可读取':'暂无可展示的批次预览');
  }
  $('spotlight-image').addEventListener('load',event=>{if(event.currentTarget.hasAttribute('src'))overviewPreviewState('ready');});
  $('spotlight-image').addEventListener('error',event=>{if(event.currentTarget.hasAttribute('src'))overviewPreviewState('failed');});
  function renderArtOverview(art,requests){
    const pending=requests.filter(item=>item.can_resolve&&!item.decision&&item.preview_kind!=='technical'),item=pending[0]||requests.find(item=>item.status==='active')||requests[0];
    const accepted=item?.status==='accepted';
    $('nav-art-count').hidden=!pending.length;text('nav-art-count',pending.length);
    text('spotlight-state',pending.length?pending.length+' 批等待你验收':accepted?'已验收预览':art.label||'尚未配置');
    $('spotlight-state').dataset.review=String(pending.length>0);
    $('spotlight-state').dataset.preview=accepted?'accepted':'';
    text('spotlight-title',item?.title||'暂无美术任务');
    text('spotlight-description',pending.length?'核对造型、材质和细节。接受后自动排队入库。':
      art.blocking_summary||art.error||art.last_error||(accepted?'配图为该批次的验收预览。':'制作和独立检查完成后显示批次预览。'));
    $('spotlight-budget').hidden=!item;
    if(item?.budget){const b=item.budget;text('spotlight-budget','已用 '+b.actual+' / '+b.limit+' 积分 · 剩余 '+b.remaining+' 积分');}
    else text('spotlight-budget',item?'本批累计费用待核实':'生成任务与预算在美术工作室查看');
    text('spotlight-action',pending.length?'查看待验收模型 →':'查看美术进展 →');
    const shot=item?.previews?.find(shot=>typeof shot.url==='string'&&shot.url.startsWith('/api/art/preview?'));
    const image=$('spotlight-image');
    if(!shot){
      image.removeAttribute('src');overviewPreviewState('empty');
    }else{
      const view=previewName(shot.name);
      image.alt=(item.title||'美术批次')+' · '+view;
      text('spotlight-source',[item.batch_id||item.id,view].filter(Boolean).join(' · '));
      if(image.getAttribute('src')!==shot.url){
        overviewPreviewState('loading');image.src=shot.url;
      }
    }
  }

  function renderArt(snapshot){
    const art=snapshot.art||{},requests=Array.isArray(art.requests)?art.requests:[];
    renderArtApprovalEntry(requests);
    renderArtDiagnosis(art);
    syncArtReworkContext(snapshot);
    text('art-status',art.label||(art.configured?'等待状态':'尚未配置'));
    const retry=art.provider_retry,waiting=art.status==='waiting_provider'&&retry?.status==='waiting';
    $('art-status').dataset.tone=art.status==='blocked'?'blocked':waiting||art.paused?'waiting':
      ['code','art_check','art_review','active'].includes(art.status)?'running':'idle';
    text('art-worker',art.configured?((art.worker?.label||'执行器状态待核实')+(art.model?' · 制作 '+prettyModel(art.model):'')+(art.effort?' · 思考度 '+(art.effort==='high'?'高':art.effort):'')+(art.task_id?' · '+art.task_id:'')+(waiting?' · '+retry.provider+'：'+retry.reason_label+' · 下次自动尝试 '+dateTime(retry.next_retry_at)+'（北京）':'')):'独立美术执行器尚未配置。');
    const production=art.production_progress;
    $('art-production').hidden=!production;
    $('art-production').dataset.stage=production?.stage||'';
    text('art-production-label',production?(production.label+(production.asset?' · '+production.asset:'')):'');
    text('art-production-time',production?'步骤更新 '+dateTime(production.updated_at)+'（北京）':'');
    const blocked=art.status==='blocked',error=art.error||art.diagnosis_response_error||(!blocked?art.last_error:'');
    text('art-error',error||'');$('art-error').classList.toggle('visible',!!error);
    $('art-blocked').hidden=!blocked;text('art-blocking-summary',art.blocking_summary||art.last_error||'本次技术审核需要处理，请查看完整记录。');text('art-blocking-detail',art.blocking_detail||art.last_error||'暂无完整记录。');
    $('art-empty').hidden=requests.length>0;
    text('art-assets-count',requests.length?requests.length+' 个批次':'');
    renderArtCards(requests);
    renderArtOverview(art,requests);
    const promotions=Array.isArray(art.promotions)?art.promotions:[];$('art-promotions-panel').hidden=!promotions.length;
    renderList('art-promotions',promotions,item=>{
      const row=node('li','art-promotion-row'),head=node('div','art-promotion-heading');
      const status={queued:'等待自动入库',active:'正在入库',integrated:'已入库',promoted:'已进入功能流程',completed:'接入完成',
        blocked:'需要处理',failed:'接入失败',waiting_target_busy:'等待目标工作区空闲',waiting_target_clean:'等待目标工作区干净',
        waiting_target_branch:'等待目标分支',waiting_target_editor:'等待目标编辑器退出',waiting_target_advanced:'等待目标版本核对'}[item.status]||item.status;
      const tone=['integrated','promoted','completed'].includes(item.status)?'good':['blocked','failed'].includes(item.status)?'warn':'';
      head.append(node('strong','',item.batch_id||item.source_task_id||item.id),node('span','tag '+tone,status));
      row.append(head);
      if(item.message)row.append(node('p','',item.message));
      if(item.head_sha)row.append(node('p','hash','提交 '+shortSHA(item.head_sha)));
      return row;
    });
    const backlog=Array.isArray(art.backlog)?art.backlog:[];$('art-backlog-panel').hidden=!backlog.length&&!art.backlog_error;
    text('art-backlog-count',backlog.length+' 个批次');text('art-backlog-error',art.backlog_error||'');$('art-backlog-error').hidden=!art.backlog_error;
    const expandedPlans=new Set([...$('art-backlog').querySelectorAll('details[open]')].map(el=>el.dataset.batch));
    renderList('art-backlog',backlog,item=>{const row=node('details','art-plan-row');row.dataset.batch=item.id;row.open=expandedPlans.has(item.id);
      const summary=node('summary');summary.append(node('span','small','优先级 '+item.priority),node('strong','',item.title),node('span','tag warn',item.status==='waiting_sample_acceptance'?'待样板验收':'待预算确认'));row.append(summary);
      const detail=node('div','document');detail.append(node('div','hash',item.id));
      if(item.levels?.length)detail.append(node('div','','等级：'+item.levels.join('、')));
      if(item.definition_ids?.length)detail.append(node('div','','资产定义：'+item.definition_ids.join('、')));
      row.append(detail);return row;});
    const stage=waiting?str(art.phase).toLowerCase():art.status,phase=stage==='art_review'?'art_check':stage;
    const chips=[...$('view-art').querySelectorAll('[data-art-stage]')];
    const step=chips.some(chip=>chip.dataset.artStage===phase)?phase:promotions.some(p=>['queued','active'].includes(p.status))?'promotion':'';
    for(const chip of chips){
      const active=chip.dataset.artStage===step;
      chip.classList.toggle('current',active&&!waiting);chip.classList.toggle('waiting',active&&waiting);
      if(active)chip.setAttribute('aria-current','step');else chip.removeAttribute('aria-current');
    }
  }
  function updateTimes(){
    text('clock',new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date()));
    if(!data)return;
    updateDevelopmentTime();
    const s=data.status||{},w=data.worker||{};
    const retry=data.provider_retry,nextRetry=parsed(retry?.next_retry_at);
    if(s.code==='waiting_provider'&&retry&&nextRetry){const remaining=(nextRetry.getTime()-Date.now())/1000;text('status-summary',retry.provider+' · '+retry.reason_label+' · 第 '+retry.attempt+' 次自动重试；下次 '+dateTime(nextRetry)+'（北京时间） · '+(remaining>0?duration(remaining)+'后自动重试':'即将重试，等待执行器确认'));}
    const recovery=w.recovery,recoveryAt=parsed(recovery?.next_retry_at);
    if(s.code==='waiting_filesystem'&&recovery&&recoveryAt){const remaining=(recoveryAt.getTime()-Date.now())/1000;text('status-summary','文件暂被占用，自动重试 · 第 '+recovery.attempt+' 次恢复；下次 '+dateTime(recoveryAt)+'（北京时间） · '+(remaining>0?duration(remaining)+'后继续原任务':'即将恢复，等待执行器确认'));}
    const last=age(s.last_activity_at,s.last_activity_age_seconds);
    text('last-activity',last===null?'暂无记录':duration(last)+'前');
    $('last-activity').title=s.last_activity_at?dateTime(s.last_activity_at,true):'';
    const heartbeat=age(w.heartbeat_at,w.heartbeat_age_seconds);
    text('heartbeat',heartbeat===null?'—':duration(heartbeat));
    const run=number(s.elapsed_seconds);
    const running=connected&&stepRunning(s,w);
    text('elapsed',running?duration(age(s.started_at)):duration(run));
    text('last-sync','最近同步 '+dateTime(receivedAt)+' · '+(connected?'每 3 秒自动更新':'数据已过期，正在重试'));
    if(!connected)text('notice','连接中断，当前保留 '+dateTime(receivedAt)+' 的最后数据（'+duration((Date.now()-receivedAt)/1000)+'前）。每 3 秒自动重试。'+(lastError?' '+lastError:''));
  }
  function filterHistory(){
    const query=$('history-search').value.trim().toLowerCase(),rows=$('accepted').children;
    let visible=0;
    for(const row of rows){
      row.hidden=!!query&&!row.dataset.search.includes(query);
      if(!row.hidden)visible++;
    }
    text('accepted-count',query?'匹配 '+visible+' / '+rows.length+' 项':'共 '+rows.length+' 项');
    $('accepted-empty').hidden=visible>0;
    text('accepted-empty',query?'没有符合条件的验收记录。':'暂无已验收任务记录。');
  }
  $('history-search').addEventListener('input',filterHistory);

  function historyCell(label,value,css=''){
    const cell=node('td',css);
    cell.setAttribute('role','cell');
    if(label)cell.dataset.caption=label;
    if(value instanceof Node)cell.append(value);
    else if(value!=null)cell.textContent=str(value);
    return cell;
  }
  function historyTime(value){
    const date=parsed(value),time=node('time','',dateTime(value));
    if(date)time.dateTime=date.toISOString();
    return time;
  }
  function historyTask(task,state,waiting=false){
    const cell=historyCell(),marks=node('div','history-task-mark'),outcome=node('span','history-outcome',state);
    if(waiting)outcome.dataset.tone='wait';
    marks.append(node('span','task-table-id hash',task.id),outcome);
    let title=task.title||task.id||'未命名任务';
    if(task.id&&title.startsWith(task.id)&&/^[：:\s]/.test(title.slice(task.id.length)))
      title=title.slice(task.id.length).replace(/^[：:\s]+/,'')||task.id;
    cell.append(marks,node('div','accepted-title',title));
    return cell;
  }
  function renderHistory(snapshot){
    const accepted=Array.isArray(snapshot.accepted)?snapshot.accepted:[];
    const deferred=Array.isArray(snapshot.deferred)?snapshot.deferred:[];
    const latest=accepted.at(-1);
    text('history-accepted-total',accepted.length);text('history-deferred-total',deferred.length);
    text('history-latest',latest?dateTime(latest.accepted_at):'暂无记录');
    $('recent-accepted').hidden=!latest;
    if(latest){text('recent-title',latest.title||latest.id);text('recent-meta',dateTime(latest.accepted_at)+' · '+shortSHA(latest.sha));}
    $('deferred-panel').hidden=deferred.length===0;text('deferred-count','共 '+deferred.length+' 项');
    renderList('deferred',[...deferred].reverse(),entry=>{
      const row=node('tr'),task=historyTask(entry,entry.status==='pending_manual_confirmation'?'待手动确认':'状态待核实',true);
      row.setAttribute('role','row');
      if(entry.reason)task.append(node('p','history-deferred-reason',entry.reason));
      const sha=historyCell('保留提交',shortSHA(entry.head_sha),'history-sha hash');
      sha.title=str(entry.head_sha);
      row.append(task,sha,historyCell('记录时间',historyTime(entry.deferred_at),'history-date'));
      return row;
    });
    const openReviews=new Set([...$('accepted').querySelectorAll('details[open]')].map(el=>el.dataset.taskId));
    renderList('accepted',[...accepted].reverse(),entry=>{
      const row=node('tr'),task=historyTask(entry,'已验收');
      row.setAttribute('role','row');
      row.dataset.search=[entry.id,entry.title,entry.sha].filter(Boolean).join(' ').toLowerCase();
      if(entry.review_summary){
        const details=node('details','review');details.dataset.taskId=str(entry.id||entry.sha);
        details.open=openReviews.has(details.dataset.taskId);
        details.append(node('summary','','查看验收摘要'),node('p','',entry.review_summary));task.append(details);
      }
      const sha=historyCell('提交 SHA',shortSHA(entry.sha),'history-sha hash');sha.title=str(entry.sha);
      const reworks=number(entry.reworks);
      row.append(task,sha,historyCell('验收时间',historyTime(entry.accepted_at),'history-date'),
        historyCell('返工',reworks===null?'未记录':reworks+' 次','history-reworks'));
      return row;
    });
    filterHistory();
  }

  function renderRuntimeSettings(snapshot,codingProvider,codingName,planningName,active){
    const worker=snapshot.worker||{},sch=snapshot.schedule||{},status=snapshot.status||{};
    const auth=snapshot.codex_auth||{},models=snapshot.models||{},route=snapshot.review_route;
    const stages=Array.isArray(snapshot.workflow?.nodes)?snapshot.workflow.nodes:[];
    const phase=status.phase,isReview=['REVIEW','MILESTONE_REVIEW'].includes(phase);
    text('settings-branch',snapshot.project?.branch);
    text('settings-window',sch.always_on===true?'每天全天':sch.weekday_start&&sch.weekday_end?'工作日 '+sch.weekday_start+'–'+sch.weekday_end+(sch.weekends_all_day===true?' · 周末全天':''):'等待排期');
    const health=worker.alive===true?(worker.healthy===true?'后台在线':'心跳待核实'):worker.alive===false?'后台离线':'等待同步';
    text('settings-worker',health);
    $('settings-worker-cell').dataset.health=worker.healthy===true?'healthy':worker.alive===true?'waiting':worker.alive===false?'offline':'unknown';
    const code=models[codingProvider==='codex'?'codex_coding':codingProvider],verify=models.codex;
    const planner=models[snapshot.planning_provider||'codex'];
    const roles=[
      ['routine_plan','settings-routine-provider','settings-routine-model','settings-routine-state',planningName,planner],
      ['critical_plan','settings-critical-provider','settings-critical-model','settings-critical-state',planningName,null],
      ['code','dsh-name','dsh-detail','dsh-state',codingName,code],
      [phase==='MILESTONE_REVIEW'?'milestone_review':'review','codex-name','codex-detail','codex-state','Codex',verify]
    ];
    const retry=snapshot.provider_retry;
    const retryLabel={local_cli_unavailable:'待重试 · 本地 CLI 恢复后继续',claude_subscription_limit:'等待 Claude 额度恢复',codex_subscription_limit:'等待 Codex 额度恢复'};
    for(const [id,providerId,specId,stateId,provider,fallback] of roles){
      const stage=stages.find(item=>item.id===id);
      const hasConfig=!!stage?.configured_model;
      const configured=hasConfig?modelWithEffort(stage.configured_model,stage.configured_effort):modelWithEffort(fallback?.model,fallback?.effort);
      const isActive=active&&stage?.current&&stage.state==='running';
      const isRetry=status.code==='waiting_provider'&&stage?.current&&retry?.provider===provider;
      const isRecovering=status.code==='waiting_filesystem'&&stage?.current;
      const actual=isActive&&stage.model_source==='recorded'?modelWithEffort(stage.model,stage.effort):'';
      text(providerId,provider);
      text(specId,configured||'模型未记录');
      text(stateId,isRecovering?'等待状态文件恢复':isRetry?(retryLabel[retry.reason]||'待重试 · 服务恢复后继续'):
        isActive?(actual&&actual!==configured?'正在执行 · '+actual:'正在执行'):stage?.state==='not_required'?'本轮无需':'');
      $(stateId).hidden=!isRecovering&&!isRetry&&!isActive&&stage?.state!=='not_required';
      $(stateId).closest('.settings-model').dataset.executing=String(!!isActive);
      setTone(stateId,isRecovering||isRetry?'warn':isActive?'working':'','model-state');
    }
    const policyLabel={responses_api:'OpenAI Responses API',api_then_subscription:'API 优先 · 订阅兜底',api_key:'GPT API',
      inherit:'本机 Codex 登录',chatgpt:'Codex 订阅'}[auth.policy];
    text('settings-review-access',route?(route.fallback_enabled?'Responses API 优先 → Codex 订阅兜底':'OpenAI Responses API'):
      isReview&&policyLabel?policyLabel:'未记录审核接入');
    text('settings-coding-access',codingProvider==='codex'?'Codex 原生订阅':codingName);
    const currentLabel={chatgpt:'Codex 订阅',responses_api:'OpenAI Responses API',api_key:'GPT API'}[auth.current];
    const apiProgress=snapshot.api_review;
    const rounds=auth.current==='responses_api'&&Number.isInteger(apiProgress?.completed_response_rounds)?' · 已完成 '+apiProgress.completed_response_rounds+' 轮审核':'';
    const apiStatus=apiProgress?.fallback_pending?' · 正在切换至 Codex 订阅':apiProgress?.api_status==='failed'?' · 等待重试':apiProgress?.api_status==='completed'?' · 本次已结束':'';
    $('codex-auth').hidden=!isReview||!currentLabel;
    text('codex-auth',currentLabel?(status.attempt_finished?'上次调用 · ':'当前调用 · ')+currentLabel+(auth.fallback?' · API 不可用后接管':'')+rounds+(rounds?apiStatus:''):'');
    $('codex-auth').title=rounds&&apiProgress.last_receipt_time?'最近回执 · '+dateTime(apiProgress.last_receipt_time,true):'';
  }

  function render(next){
    data=next;receivedAt=Date.now();
    const p=data.project||{},s=data.status||{},w=data.worker||{},sch=data.schedule||{},t=data.task;
    text('project-name',p.name||'未配置项目');text('branch',p.branch);text('milestone',p.milestone);text('workspace',p.workspace);text('source',p.source);
    $('copy-workspace').disabled=!p.workspace;$('copy-source').disabled=!p.source;
    const waitingWithoutOperation=s.code==='waiting_window'&&!s.started_at&&!data.operation&&!s.operation;
    const providerWaiting=s.code==='waiting_provider';
    const filesystemWaiting=s.code==='waiting_filesystem';
    const phase=phaseNames[s.phase]||s.phase||'等待任务';
    const executionConfirmed=stepRunning(s,w);
    const waitingToRetry=s.code===str(s.phase).toLowerCase()&&number(s.failures)>0&&!executionConfirmed&&w.healthy===true;
    const tone=['blocked','stopped','error'].includes(s.code)?'bad':
      waitingToRetry||['paused','heartbeat_stale','waiting_window','waiting_provider','waiting_filesystem'].includes(s.code)?'warn':'good';
    text('phase-tag',waitingToRetry?'等待后台重试':s.label||phase);setTone('phase-tag',tone,'tag');$('status-panel').dataset.tone=tone;
    for(const [key,el] of phaseElements){
      const active=key===s.phase&&executionConfirmed&&!providerWaiting&&!filesystemWaiting;
      el.classList.toggle('active',active);el.classList.toggle('pending',key===s.phase&&!active);
      if(key===s.phase)el.setAttribute('aria-current','step');else el.removeAttribute('aria-current');
    }
    const start=sch.weekday_start||'18:00';
    let taskTitle=t?str(t.title||t.id||'未命名任务'):'';
    if(t?.id&&taskTitle.startsWith(t.id)&&/^[：:\s]/.test(taskTitle.slice(t.id.length))){
      taskTitle=taskTitle.slice(t.id.length).replace(/^[：:\s]+/,'')||t.id;
    }
    text('task-id',t?.id||'当前运行');
    text('status-title',taskTitle||s.label||'等待任务');
    text('status-summary',s.summary===s.error?'当前步骤 · '+phase:s.summary||'当前步骤 · '+phase);
    text('actor',executionConfirmed?s.actor:'—');
    text('overview-subtitle',[p.name,p.milestone&&'里程碑 '+p.milestone,sch.always_on===true?'全天运行':'按工作窗口运行'].filter(Boolean).join(' · '));
    const err=[s.error,data.error].filter(Boolean).map(str).map(message=>errorLabels[message]||message);
    if(s.previous_error)err.push('上次调用记录：'+(errorLabels[s.previous_error]||str(s.previous_error)));
    const retryingStep=executionConfirmed&&number(s.failures)>0;
    if(retryingStep&&err.length)err.unshift('正在续跑，以下为上次尝试的记录。');
    text('status-error',[...new Set(err)].join('\n'));
    $('status-error').classList.toggle('visible',err.length>0);
    $('status-error').dataset.retrying=String(!!s.previous_error||retryingStep||waitingToRetry);
    text('worker-title',w.alive===true?(w.healthy===true?'后台在线':'心跳待核实'):w.alive===false?'后台离线':'状态未知');
    const healthTone=w.alive===false?'bad':w.alive===true?(w.healthy===true?'good':'warn'):'';
    setTone('worker-dot',healthTone,'dot');setTone('worker-title',healthTone,'health-title');text('pid',w.pid);
    text('failures',number(s.failures));setTone('failures',number(s.failures)>0?'warn':'');
    $('worker-desc').hidden=!w.label||w.healthy===true;
    text('worker-desc',w.label);
    const codingProvider=['claude','codex'].includes(data.coding_provider)?data.coding_provider:'dsh';
    const codingName={claude:'Claude Code',codex:'Codex',dsh:'DSH'}[codingProvider];
    const planningName=data.planning_provider==='claude'?'Claude Code':'Codex';
    const [action,detail]=waitingToRetry?['自动重试当前步骤','后台会从'+phase+'继续。']:nextAction(s,w,sch,phase,codingName,planningName);
    text('next-action',action);text('next-detail',detail);
    text('coder-brand',codingProvider==='codex'?'Codex':'Codex × '+codingName);text('coder-avatar',{claude:'CC',codex:'CX',dsh:'d'}[codingProvider]);
    document.title=viewLabels[activeView]+' · 无人值守工作台';
    renderRuntimeSettings(data,codingProvider,codingName,planningName,executionConfirmed);
    const b=data.burst,burstEnded=!!b&&['completed','done'].includes(str(b.status).toLowerCase());$('burst').hidden=!b||(burstEnded&&!waitingWithoutOperation);
    if(b&&burstEnded&&waitingWithoutOperation){const nextOpen=parsed(sch.next_open),beijingDay=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'});const resumes=nextOpen?(beijingDay.format(nextOpen)===beijingDay.format(new Date())?'今天 '+new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hour12:false}).format(nextOpen):dateTime(nextOpen))+' 自动继续':'下次工作窗口自动继续';text('burst',(b.ignore_window===true?'试跑':'手动批次')+' '+(b.completed_tasks??'—')+'/'+(b.requested_tasks??'—')+' 已完成，已恢复原排期，'+resumes+'。');setTone('burst','good','burst');}
    else if(b){text('burst','批次 '+(b.completed_tasks??'—')+' / '+(b.requested_tasks??'—')+' 个任务完成 · 剩余 '+(b.remaining_tasks??'—')+(b.status?' · '+b.status:''));setTone('burst','','burst');}
    $('task-content').hidden=!t;$('task-empty').hidden=!!t;text('task-title',t?.id);
    if(t){text('task-body',t.body||'暂无任务书内容。');text('reworks','返工 '+(t.reworks??0)+' 次');text('base-sha','基线 '+shortSHA(t.base_sha));text('head-sha','当前 '+shortSHA(t.head_sha));$('base-sha').title=str(t.base_sha);$('head-sha').title=str(t.head_sha);const criteria=Array.isArray(t.acceptance_criteria)?t.acceptance_criteria:t.acceptance_criteria?[t.acceptance_criteria]:['暂无验收条件。'];renderList('criteria',criteria,item=>node('li','',item));}
    if(t){const session=t.session_owner_task_id===t.id?str(t.session_id):'';const sessionLabel=(t.session_provider||codingName)+' 独立会话';text('task-session',sessionLabel+' · '+(session?(session.length>18?session.slice(0,18)+'…':session)+' · 归属 '+str(t.id):'等待新会话建立'));$('task-session').title=session?'任务 '+str(t.id)+' · '+sessionLabel+' '+session:'';}
    else{$('task-empty').replaceChildren(node('strong','',sch.allowed===false?'等待 '+start+' 工作窗口':'当前没有执行中的任务'),node('span','',sch.allowed===false?'后台按排期等待，最近活动与心跳仍持续更新。':'下一项任务启动后，任务书与验收条件将显示在这里。'));}
    const activity=Array.isArray(data.activity)?data.activity:[];const events=[...activity].reverse().sort((a,b)=>(parsed(b.at)?.getTime()||0)-(parsed(a.at)?.getTime()||0)).slice(0,30);$('activity-empty').hidden=events.length>0;text('activity-count',events.length?'最近 '+events.length+' 条':'暂无记录');$('activity').classList.toggle('waiting',providerWaiting||filesystemWaiting);
    renderList('activity',events,e=>{const li=node('li'),meta=node('div','event-meta'),time=node('time','event-time',parsed(e.at)?dateTime(e.at):'本轮记录');if(parsed(e.at))time.dateTime=parsed(e.at).toISOString();meta.append(node('span','event-actor',e.actor||e.kind||'系统'),time);li.append(meta,node('div','event-text',e.text||e.kind||'活动记录'));return li;});
    text('window-tag',sch.allowed===true?'窗口已开放':sch.allowed===false?'窗口外':'排期待确认');setTone('window-tag',sch.allowed===true?'good':sch.allowed===false?'warn':'','tag');text('window-main',sch.allowed===true?'当前可以执行任务':sch.allowed===false?'等待 '+start+' 开放':'暂无排期信息');
    text('window-next',sch.allowed===true?(sch.closes_at?'本次结束 '+dateTime(sch.closes_at):'当前工作窗口已开放'):(sch.next_open?'下次开放 '+dateTime(sch.next_open):'下次开放时间待获取'));
    const windowRule=sch.always_on===true?'每天全天运行':'工作日 '+(sch.weekday_start||'—')+'–'+(sch.weekday_end||'—')+' · '+(sch.weekends_all_day===true?'周末全天':sch.weekends_all_day===false?'周末按排期':'周末规则待获取');
    text('window-rule',windowRule+'\n时区 '+(sch.timezone||'未提供'));
    const pub=data.publication||{};text('publication',(pub.summary||'发布模式待获取')+(pub.mode==='milestone'&&pub.pending_tasks!=null?' · 待发布 '+pub.pending_tasks+' 项':''));
    const notifications=data.notifications||{};text('notification-state',notifications.enabled===true?'已接入':notifications.enabled===false?'未启用':'未接入');setTone('notification-state',notifications.last_error?'warn':notifications.enabled===true?'good':'');text('notification-detail',(notifications.last_sent_at?'最近发送 '+dateTime(notifications.last_sent_at):'尚无发送记录')+(notifications.last_event?' · '+str(notifications.last_event):'')+(notifications.pending!=null?' · 待发送 '+str(notifications.pending):''));text('notification-error',notifications.last_error);$('notification-error').classList.toggle('visible',!!notifications.last_error);
    renderHistory(data);
    renderWorkflow(data);
    renderDevelopment(data);
    renderArt(data);
    renderFlows(data);
    updateTimes();
  }
  function connection(ok,error=''){
    connected=ok;lastError=error;setTone('connection-dot',ok?'good':'warn','dot');text('connection-label',ok?'实时连接':data?'连接中断 · 数据过期':'连接失败 · 重试中');$('notice').classList.toggle('visible',!ok);
    const worker=data?.worker||{};
    const [workerTone,workerLabel]=!ok?['stale','连接待恢复']:worker.alive===false?['offline','后台离线']:
      worker.healthy===true?['online','后台在线']:['stale','心跳待核实'];
    $('sidebar-worker-state').dataset.tone=workerTone;text('sidebar-worker-state',workerLabel);
    syncArtActions();
    syncArtDiagnosisActions();
    if(data){renderOverviewSteps(data);renderDevelopment(data);}
    $('workflow-panel').classList.toggle('is-stale',!ok);$('modules-grid').classList.toggle('is-stale',!ok);
    $('view-flows').classList.toggle('is-stale',!ok);$('flows-stale').hidden=ok;
    if(data)renderFlows(data);
    if(!ok&&$('flow-detail-dialog').open)text('flow-detail-status','状态连接中断 · 数据过期，正在重新同步。');
    if(!ok&&!data)text('notice','暂时无法读取本地状态，尚无可显示的数据。每 3 秒自动重试。'+(error?' '+error:''));
    updateTimes();
  }
  async function refresh(){
    if(busy)return;busy=true;$('refresh').disabled=true;
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),7000);
    try{const response=await fetch('/api/status',{cache:'no-store',signal:controller.signal,headers:{Accept:'application/json'}});if(!response.ok)throw new Error('HTTP '+response.status);const next=await response.json();if(!next||typeof next!=='object'||Array.isArray(next))throw new Error('状态响应格式无效');connected=true;render(next);connection(true);}
    catch(error){connection(false,error.name==='AbortError'?'请求超时。':str(error.message));}
    finally{clearTimeout(timeout);busy=false;$('refresh').disabled=false;}
  }
  function toast(message){text('toast',message);$('toast').classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.remove('visible'),3000);}
  for(const field of ['workspace','source'])$('copy-'+field).addEventListener('click',async()=>{const value=data?.project?.[field];if(!value)return;try{await navigator.clipboard.writeText(str(value));toast('路径已复制');}catch{const range=document.createRange();range.selectNodeContents($(field));const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);toast('剪贴板不可用，已选中路径，请按 Ctrl+C 复制');}});
  $('refresh').addEventListener('click',refresh);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  updateTimes();refresh();setInterval(refresh,3000);setInterval(updateTimes,1000);
})();
