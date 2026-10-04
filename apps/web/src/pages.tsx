import { useCallback, useEffect, useRef, useState } from "react";
import { call, getToken, getWho } from "./api";
import { Empty, Err, fmtRep, Item, Meta, Tag } from "./ui";

type Hit = { memory_id: string; matched_by: string; representation?: string };
type Mem = {
  memory_id: string; version: number; memory_date: string | null;
  representation: string; text: string; pinned: boolean;
};

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [state, setState] = useState<{ data?: T; error?: unknown; loading: boolean }>(
    { loading: true });
  const reload = useCallback(() => {
    setState({ loading: true });
    fn().then(
      (data) => setState({ data, loading: false }),
      (error) => setState({ error, loading: false }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(reload, [reload]);
  return { ...state, reload };
}

export function Memories({ note }: { note: (s: string, err?: boolean) => void }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[]>([]);
  const [detail, setDetail] = useState<Record<string, Mem>>({});
  const [error, setError] = useState<unknown>();
  const reqSeq = useRef(0);

  const search = useCallback(async (query: string) => {
    const seq = ++reqSeq.current;
    try {
      const d = await call<{ hits: Hit[]; degraded?: string }>("memory.search",
        { query });
      if (seq !== reqSeq.current) return; // 过期响应丢弃（最新请求胜出）
      setHits(d.hits);
      setError(undefined);
      if (d.degraded) note(`语义检索未配置：${d.degraded}`);
      const next: Record<string, Mem> = {};
      for (const h of d.hits) {
        next[h.memory_id] = await call<Mem>("memory.get",
          { memory_id: h.memory_id });
      }
      if (seq === reqSeq.current) setDetail(next);
    } catch (e) { if (seq === reqSeq.current) setError(e); }
  }, [note]);

  useEffect(() => { search(""); }, [search]);

  const hold = async () => {
    const text = window.prompt("记忆正文（合成测试数据）");
    if (!text) return;
    // F22（2026-10-03 审计 P2）：hold 必填 categories；日期由使用者
    // 明确给出（默认今天）——不再自动伪造"40 天前"的事件日期
    const date = window.prompt(
      "事件日期 YYYY-MM-DD", new Date().toISOString().slice(0, 10));
    if (!date) return;
    try {
      const d = await call<{ memory_id: string }>("memory.hold", {
        text, why_remember: "测试用途",
        categories: ["daily"],
        memory_date: date,
      });
      note(`已写入 ${d.memory_id}`);
      search(q);
    } catch (e) { note(String(e), true); }
  };

  return (
    <div>
      <div className="row">
        <input value={q} onChange={(e) => setQ(e.target.value)}
               onKeyDown={(e) => {
                 if (e.key === "Enter") search(e.currentTarget.value);
               }}
               placeholder="关键词，例如：蓝瓷小钥匙" style={{ flex: 1 }} />
        <button className="primary" onClick={() => search(q)}>搜索</button>
        <button onClick={() => setHits([])}>清空</button>
        <button onClick={hold}>写测试记忆</button>
      </div>
      {error ? <Err e={error} /> : null}
      {!error && !hits.length ? <Empty>无命中</Empty> : null}
      {hits.map((h) => {
        const m = detail[h.memory_id];
        return (
          <Item key={h.memory_id}>
            <Meta>
              <span>{h.memory_id}</span>
              {m ? <Tag kind={m.representation}>{fmtRep(m.representation)}</Tag> : null}
              {m ? <span>v{m.version}</span> : null}
              <span>{m?.memory_date ?? "日期未知"}</span>
            </Meta>
            <div className="detail">{m?.text ?? "…"}</div>
            <Meta>
              <span>matched_by: <code>{h.matched_by}</code></span>
              {m && m.representation === "forgotten_summary" ? (
                <span>旧遗忘摘要（v1.7 起遗忘已退役，仅供查看）</span>
              ) : null}
            </Meta>
          </Item>
        );
      })}
    </div>
  );
}

type Plan = {
  plan_id: string; title: string; state: string; version: number;
  content?: string | null;
  date_start?: string | null; date_end?: string | null;
  starts_at?: string | null; due_at?: string | null;
};

type Proposal = {
  proposal_id: string; target_memory_id: string; state: string; revision: number;
  created_by: string; proposal_hash: string; base_memory_version: number | null;
  current_memory_version: number | null; compressed_summary: string; reason: string;
};

export function Plans({ note }: { note: (s: string, err?: boolean) => void }) {
  const [title, setTitle] = useState("");
  const [due, setDue] = useState("");
  const { data, error, reload } = useAsync<{ plans: Plan[] }>(
    () => call("plan.list", {}), []);

  const add = async () => {
    if (!title.trim()) return;
    try {
      await call("plan.create", {
        title, state: "planned",
        ...(due ? { due_at: due } : {}),
      });
      setTitle(""); setDue("");
      note("计划已创建");
      reload();
    } catch (e) { note(String(e), true); }
  };
  const setState = async (p: Plan, s: string) => {
    try {
      await call("plan.update", { plan_id: p.plan_id, expected_version: p.version, state: s });
      reload();
    } catch (e) { note(String(e), true); reload(); }
  };

  return (
    <div>
      <div className="row">
        <input value={title} onChange={(e) => setTitle(e.target.value)}
               placeholder="新计划标题" style={{ flex: 1 }} />
        <input type="date" value={due} onChange={(e) => setDue(e.target.value)} />
        <button className="primary" onClick={add}>创建</button>
      </div>
      {error ? <Err e={error} /> : null}
      {data?.plans.map((p) => (
        <Item key={p.plan_id}>
          <Meta>
            <Tag kind={["active", "waiting", "blocked"].includes(p.state)
              ? "state-submitted" : p.state === "done" ? "state-approved_and_applied"
              : p.state === "cancelled" ? "state-rejected" : "state-draft"}>
              {p.state}
            </Tag>
            <span>v{p.version}</span>
            <span>{p.starts_at ?? p.date_start ?? ""}{p.due_at ? ` → ${p.due_at.slice(0, 10)}` : ""}</span>
          </Meta>
          <div className="detail"><strong>{p.title}</strong></div>
          <div className="row">
            {["active", "waiting", "blocked", "done", "cancelled"]
              .filter((s) => s !== p.state)
              .map((s) => <button key={s} onClick={() => setState(p, s)}>{s}</button>)}
          </div>
        </Item>
      ))}
    </div>
  );
}

type Quote = {
  id: string; text: string; said_at: string | null; kept_by: string;
  semantic_status: string;
};

type DelReq = {
  id: string;                // request_id
  request_id?: string;
  memory_id: string;
  human_reason: string;
  rejection_reason?: string | null;
  status: string;            // pending/approved/rejected/withdrawn/superseded
  submitted_local_date: string;
};

export function Deletions({ note }: { note: (s: string, err?: boolean) => void }) {
  // CB-052（2026-10-02 审计 P2）：按 Deletion v2.0 合同——申请走
  // memory_id + reason + operation_id（无 action：archive 已退役）；
  // reject 必填理由；withdraw 用 request_id
  const [target, setTarget] = useState("");
  const [reason, setReason] = useState("");
  const { data, error, reload } = useAsync<{ requests: DelReq[] }>(
    () => call("memory.deletion.list", {}), []);

  const submit = async () => {
    try {
      const d = await call<{ request_id: string }>(
        "memory.deletion.request", {
          memory_id: target, reason,
          operation_id: `ui-req-${target}-${Date.now()}`});
      setTarget(""); setReason("");
      note(`申请已提交 ${d.request_id}（pending，内容未变）`);
      reload();
    } catch (e) { note(String(e), true); }
  };
  const decide = async (r: DelReq, decision: string,
                        rejectionReason?: string) => {
    try {
      await call("memory.deletion.decide", {
        request_id: r.request_id || r.id, decision,
        ...(decision === "reject" && rejectionReason
          ? { rejection_reason: rejectionReason } : {}),
      }, `ui-del-${r.id}-${decision}`);
      note(decision === "approve" ? "已批准并执行删除" : "已拒绝");
      reload();
    } catch (e) { note(String(e), true); reload(); }
  };
  const withdraw = async (rid: string) => {
    try {
      await call("memory.deletion.withdraw", { request_id: rid });
      note("已撤回"); reload();
    } catch (e) { note(String(e), true); }
  };

  return (
    <div>
      <div className="row">
        <input value={target} onChange={(e) => setTarget(e.target.value)}
               placeholder="memory_id" style={{ flex: 1 }} />
        <input value={reason} onChange={(e) => setReason(e.target.value)}
               placeholder="理由（必填）" style={{ flex: 1 }} />
        <button className="danger" onClick={submit}>提交删除申请</button>
      </div>
      <Empty>v2.0：申请即物理删除诉求（archive 已退役）；理由必填；每日 10 条；每资源 lifetime 5 次；仅周家明审批；审批前内容不变。</Empty>
      {error ? <Err e={error} /> : null}
      {data?.requests.slice(0, 30).map((r) => (
        <Item key={r.id}>
          <Meta>
            <span>{r.id}</span>
            <Tag kind={`state-${r.status}`}>{r.status}</Tag>
            <span>{r.memory_id}</span>
            <span>{r.submitted_local_date}</span>
          </Meta>
          <div className="detail">理由：{r.human_reason}
            {r.rejection_reason ? <><br />拒绝理由：{r.rejection_reason}</> : null}</div>
          {r.status === "pending" ? (
            <div className="row">
              <button className="primary" onClick={() => decide(r, "approve")}>批准（物理删除）</button>
              <button className="danger" onClick={() =>
                decide(r, "reject",
                       window.prompt("拒绝理由（必填）") || undefined)
              }>拒绝</button>
              <button onClick={() => withdraw(r.id)}>撤回</button>
            </div>
          ) : null}
        </Item>
      ))}
    </div>
  );
}

type HomeData = { version: number; content: string | null };
type SelfEntry = {
  self_id: string; aspect: string; version: number; review_state: string;
  written_at: string; review_available_on: string; content: string;
};
type DiaryEntry = {
  diary_id: string; title: string; author: string; version: number;
  covers_from: string | null; covers_to: string | null; content: string;
};

type MediaObj = {
  content_hash: string; mime: string; size: number; created_by: string;
  created_at: string; owned_by?: string;
};

export function MediaLib({ note }: { note: (s: string, e?: boolean) => void }) {
  const { data, error, reload } = useAsync<{ objects: MediaObj[] }>(
    () => call("media.list", {}), []);
  const [file, setFile] = useState<File | null>(null);

  const upload = async () => {
    if (!file) return;
    try {
      const prep = await call<{ upload_token: string; stage_url: string }>(
        "media.upload.prepare", { mime: file.type || "image/png", size: file.size });
      const stageRes = await fetch(prep.stage_url, {
        method: "PUT", headers: { "Authorization": `Bearer ${getToken()}` },
        body: await file.arrayBuffer(),
      });
      if (!stageRes.ok) throw new Error(`stage ${stageRes.status}`);
      const buf = new Uint8Array(await file.arrayBuffer());
      let bin = "";
      for (const b of buf) bin += String.fromCharCode(b);
      const out = await call<{ content_hash: string; deduplicated: boolean }>(
        "media.upload.finalize",
        { upload_token: prep.upload_token, data_b64: btoa(bin) });
      note(out.deduplicated ? "已存在（hash 去重）" : `已上传 ${out.content_hash.slice(0, 12)}…`);
      setFile(null); reload();
    } catch (e) { note(String(e), true); }
  };

  return (
    <div>
      <div className="row">
        <input type="file" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        <button className="primary" onClick={upload}>上传（两步+hash 去重）</button>
      </div>
      {error ? <Err e={error} /> : null}
      {data?.objects.length ? data.objects.map((m) => (
        <Item key={m.content_hash}>
          <Meta>
            <code>{m.content_hash.slice(0, 16)}…</code>
            <span>{m.mime}</span><span>{m.size}B</span>
            <span>{m.owned_by}</span><span>{m.created_at.slice(0, 10)}</span>
          </Meta>
          <img src={`/api/media/object/${m.content_hash}`} alt="" style={{ maxWidth: 120, marginTop: 6 }} />
        </Item>
      )) : <Empty>暂无媒体对象</Empty>}
    </div>
  );
}

type Settings = Record<string, unknown>;

export function SettingsPage() {
  const { data, error } = useAsync<Settings>(
    () => call("maintenance.settings.get", {}), []);
  if (error) return <Err e={error} />;
  if (!data) return <Empty>加载中…</Empty>;
  const section = (title: string, obj: unknown) => (
    <Item key={title}>
      <Meta><strong>{title}</strong></Meta>
      <pre className="hash">{JSON.stringify(obj, null, 2)}</pre>
    </Item>
  );
  return (
    <div>
      {Object.entries(data).filter(([, v]) => typeof v === "object" && v !== null)
        .map(([k, v]) => section(k, v))}
      {Object.entries(data).filter(([, v]) => typeof v !== "object")
        .map(([k, v]) => (
          <Item key={k}>
            <Meta><span>{k}</span><strong>{String(v)}</strong></Meta>
          </Item>
        ))}
      <Empty>只读快照；修改经部署参数（policy version 变更入审计）。</Empty>
    </div>
  );
}
