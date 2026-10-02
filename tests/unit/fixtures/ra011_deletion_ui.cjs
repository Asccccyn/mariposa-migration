// RA-011（2026-10-02 复审）：当前真实 index.html 删除流程按 v2.0 合同
// 回调（memory_id+operation_id、无 action、reject 理由、withdraw
// request_id）。离线 VM 执行，无浏览器/网络。
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const html = fs.readFileSync(
    path.join(__dirname, '..', '..', '..',
              'backend', 'mariposa', 'web', 'index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
const elements = new Map();
function element(id = '') {
    return {id, value: id.startsWith('del') ? 'mem_x' : '', innerHTML: '',
            style: {}, children: [], prompt_ans: null,
            classList: {add() {}, remove() {}},
            addEventListener() {}, appendChild(e) {this.children.push(e)},
            insertAdjacentHTML(_, s) {this.innerHTML += s},
            remove() {}, scrollIntoView() {}};
}
const requests = [];
const ctx = vm.createContext({
    console, Date,
    document: {
        getElementById(id) {
            if (!elements.has(id)) elements.set(id, element(id));
            return elements.get(id);
        },
        querySelectorAll() { return []; },
        createElement() { return element(); },
    },
    localStorage: {getItem() {return '';}, setItem() {}},
    prompt() { return '拒绝理由'; },
    fetch() { throw Error('FORBIDDEN'); },
    crypto: {randomUUID() {return 'x';}},
    setTimeout() {},
});
vm.runInContext(script, ctx, {timeout: 1000});
ctx.capture = async (name, args) => {
    requests.push({name, args});
    return {requests: [], request_id: 'req_1'};
};
vm.runInContext('api = capture', ctx, {timeout: 1000});
// 提交申请（元素由 onclick 内部 $() 触发惰性创建——先 touch）
ctx.document.getElementById('delTarget');
ctx.document.getElementById('delReason');
ctx.document.getElementById('delSubmit');
elements.get('delReason').value = '测试理由';
(async () => {
    await elements.get('delSubmit').onclick();
    const req = requests.find(r => r.name === 'memory.deletion.request');
    const v2 = req && 'memory_id' in req.args && 'operation_id' in req.args
        && !('action' in req.args) && !('resource_id' in req.args);
    console.log(JSON.stringify({case: 'ra011-request-v2',
                                v2_contract: !!v2}));
    if (!v2) process.exitCode = 1;
})().catch(e => {console.error(e); process.exitCode = 1;});
