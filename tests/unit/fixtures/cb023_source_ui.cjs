// CB-023/CB-053（2026-10-02 审计）离线回归：当前真实 index.html 的
// Source 分页方向与默认写入流程。方式与审计取证一致：VM 执行页面
// <script>，无浏览器/服务/网络。
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const html = fs.readFileSync(
    path.join(__dirname, '..', '..', '..',
              'backend', 'mariposa', 'web', 'index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

const elements = new Map();
function element(id = '') {
    return {id, value: id === 'holdCat' ? 'daily' : '', innerHTML: '',
            style: {}, children: [],
            classList: {add() {}, remove() {}},
            addEventListener() {}, appendChild(e) {this.children.push(e)},
            insertAdjacentHTML(_, s) {this.innerHTML += s},
            remove() {}, scrollIntoView() {}};
}
class FixedDate extends Date {
    static now() { return Date.parse('2026-10-02T12:00:00Z'); }
}
const requests = [];
const prompts = [];
// 2026-10-05 标题必填：prompt 顺序=标题、正文、日期（标题非空、
// 正文非空、日期留空）
const promptAnswers = ['CB-053 标题', 'CB-053 合成正文', ''];
const ctx = vm.createContext({
    console, Date: FixedDate,
    document: {
        getElementById(id) {
            if (!elements.has(id)) elements.set(id, element(id));
            return elements.get(id);
        },
        querySelectorAll() { return []; },
        createElement() { return element(); },
    },
    localStorage: {getItem() {return '';}, setItem() {}},
    prompt(v) {
        prompts.push(String(v));
        return promptAnswers[Math.min(prompts.length - 1,
                                      promptAnswers.length - 1)];
    },
    fetch() { throw Error('AUDIT_NETWORK_FORBIDDEN'); },
    crypto: {randomUUID() {return 'synthetic-id';}},
    setTimeout() {},
});
vm.runInContext(script, ctx, {timeout: 1000});
ctx.capture = async (name, args) => {
    requests.push({name, args});
    return {messages: [], hits: []};
};
vm.runInContext('api = capture', ctx, {timeout: 1000});

// ---- CB-023：101 条、首屏后 has_more——续页必须用 next_after_cursor
//（旧实现发 before_seq=0，第 101 条不可达）
(async () => {
ctx.response = {
    conversation: {title: 't', message_count: 101,
                   provider_conversation_id: 'p'},
    messages: [], has_more: true,
    prev_before_seq: 0,
    next_after_cursor: {seq: 99, id: 'last-id'},
};
vm.runInContext(
    'srcState.conv="synthetic";renderSourceConv(response,false)',
    ctx, {timeout: 1000});
const box = elements.get('srcConvView');
const button = box.children.at(-1).children[0];
await button.onclick();
ctx.response = {conversation: {}, messages: [], has_more: false};
const page = requests.find(
    r => r.name === 'source.conversation.get');
const usedAfterCursor = page && 'after_cursor' in page.args
    && page.args.after_cursor.seq === 99
    && page.args.after_cursor.id === 'last-id';
const usedBeforeSeq = page && 'before_seq' in page.args;
console.log(JSON.stringify({
    case: 'cb023-ui-pagination-direction',
    used_after_cursor: !!usedAfterCursor,
    used_before_seq: !!usedBeforeSeq,
}));
if (!usedAfterCursor || usedBeforeSeq) process.exitCode = 1;

// ---- CB-053：默认写入不再伪造 40 天前的 exact 日期——空输入提交
// unknown/null（旧实现固定提交 2026-08-23/exact）
prompts.length = 0;
requests.length = 0;
await elements.get('newHoldBtn').onclick();
const hold = requests.find(r => r.name === 'memory.hold');
const fixedDate = hold && hold.args.memory_date === '2026-08-23'
    && hold.args.date_confidence === 'exact';
const unknownDate = hold && (hold.args.memory_date === null
    || hold.args.memory_date === undefined)
    && hold.args.date_confidence === 'unknown';
console.log(JSON.stringify({
    case: 'cb053-ui-hold-date',
    fixed_test_fixture_submitted: !!fixedDate,
    unknown_when_no_input: !!unknownDate,
}));
if (fixedDate || !unknownDate) process.exitCode = 1;
})().catch(e => {console.error(e); process.exitCode = 1;});
