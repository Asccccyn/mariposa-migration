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
    try {
      const d = await call<{ memory_id: string }>("memory.hold", {
        text, why_remember: "测试用途",
        memory_date: new Date(Date.now() - 40 * 86400000).toISOString().slice(0, 10),
      });
      note(`已写入 ${d.memory_id}`);
      search(q);
    } catch (e) { note(String(e), true); }
  };

  const restore = async (id: string, ver: number) => {
    try {
      const d = await call<{ new_version: number }>("memory.restore", {
        memory_id: id, expected_current_version: ver });
      note(`已恢复到 v${d.new_version}`);
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
        <button onClick={() => call("workspace.forgetting.scan", {})
          .then((d) => { note(`扫描完成：新建草稿 ${(d as { created: unknown[] }).created.length}`); })
          .catch((e) => note(String(e), true))}>扫描遗忘候选</button>
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
                <button onClick={() => restore(m.memory_id, m.version)}>恢复旧正文</button>
              ) : null}
            </Meta>
          </Item>
        );
      })}
    </div>
  );
}

type Proposal = {
  proposal_id: string; target_memory_id: string; state: string; revision: number;
  created_by: string; proposal_hash: string; base_memory_version: number | null;
  current_memory_version: number | null; compressed_summary: string; reason: string;
};

export function Workspace({ note }: { note: (s: string, err?: boolean) => void }) {
  const { data, error, reload } = useAsync<{ items: Proposal[] }>(
    () => call("workspace.proposals.list", {}), []);
  const [drafts, setDrafts] = useState<Record<string, { sum: string; rsn: string }>>({});
  // 冻结稿信息：revise 返回的 revision/hash 供 submit/decide 精确引用
  const [frozen, setFrozen] = useState<Record<string, { revision: number; hash: string }>>({});

  if (error) return <Err e={error} />;
  if (!data) return <Empty>加载中…</Empty>;
  if (!data.items.length) return <Empty>暂无工作项（先「扫描遗忘候选」）</Empty>;

  const setDraft = (id: string, sum: string, rsn: string) =>
    setDrafts((d) => ({ ...d, [id]: { sum, rsn } }));

  const act = async (kind: string, p: Proposal) => {
    try {
      if (kind === "revise") {
        const d = await call<{ revision: number; payload_hash: string }>(
          "workspace.proposals.revise", {
          proposal_id: p.proposal_id,
          compressed_summary: drafts[p.proposal_id]?.sum ?? p.compressed_summary,
          reason: drafts[p.proposal_id]?.rsn ?? p.reason,
        });
        setFrozen((f) => ({ ...f, [p.proposal_id]:
          { revision: d.revision, hash: d.payload_hash } }));
        note(`修订到 r${d.revision}`);
      } else if (kind === "submit") {
        const info = frozen[p.proposal_id];
        await call("workspace.proposals.submit", {
          proposal_id: p.proposal_id,
          proposal_revision: info ? info.revision : p.revision,
          proposal_hash: info ? info.hash : p.proposal_hash,
        }, `ui-submit-${p.proposal_id}-${p.revision}`);
        note("已提交，hash 冻结");
      } else if (kind === "approve" || kind === "reject") {
        await call("memory.forgetting.decide", {
          proposal_id: p.proposal_id, proposal_revision: p.revision,
          proposal_hash: p.proposal_hash,
          expected_memory_version: p.base_memory_version ?? 0,
          decision: kind === "approve" ? "approve" : "reject",
        }, `ui-decide-${p.proposal_id}-${p.revision}-${kind}`);
        note(kind === "approve" ? "已批准并应用" : "已拒绝");
      }
      reload();
    } catch (e) { note(String(e), true); reload(); }
  };

  return (
    <div>
      {data.items.map((p) => {
        const stale = p.state === "submitted" &&
          p.base_memory_version !== null && p.current_memory_version !== null &&
          p.base_memory_version !== p.current_memory_version;
        const d = drafts[p.proposal_id];
        return (
          <Item key={p.proposal_id}>
            <Meta>
              <span>{p.proposal_id}</span><span>r{p.revision}</span>
              <span>{p.created_by}</span>
              <Tag kind={`state-${p.state}`}>{p.state}</Tag>
              {stale ? <Tag>STALE</Tag> : null}
            </Meta>
            <div>目标：<code>{p.target_memory_id}</code>
              {" "}base v{p.base_memory_version ?? "?"} / 当前 v{p.current_memory_version ?? "?"}</div>
            {p.state === "draft" ? (
              <>
                <textarea value={d?.sum ?? p.compressed_summary}
                  onChange={(e) => setDraft(p.proposal_id, e.target.value, d?.rsn ?? p.reason)}
                  placeholder="审批后作为唯一检索依据的摘要" />
                <textarea value={d?.rsn ?? p.reason}
                  onChange={(e) => setDraft(p.proposal_id, d?.sum ?? p.compressed_summary, e.target.value)}
                  placeholder="理由" />
                <div className="row">
                  <button onClick={() => act("revise", p)}>保存修订</button>
                  <button className="primary" onClick={() => act("submit", p)}>提交</button>
                </div>
              </>
            ) : p.state === "submitted" ? (
              <>
                <div className="detail">{p.compressed_summary || "(无摘要)"}</div>
                <pre className="hash">hash {p.proposal_hash}</pre>
                <div className="row">
                  <button className="primary" onClick={() => act("approve", p)}>批准</button>
                  <button className="danger" onClick={() => act("reject", p)}>拒绝</button>
                </div>
              </>
            ) : (
              <div className="detail">{p.compressed_summary}</div>
            )}
          </Item>
        );
      })}
    </div>
  );
}

type CalItem = {
  item_id: string; kind: string; date_start: string; date_end: string;
  title: string; preview: string; preview_kind: string; status: string;
};

export function Calendar() {
  const now = new Date();
  const [ym, setYm] = useState({ y: now.getFullYear(), m: now.getMonth() });
  const [sel, setSel] = useState<string | null>(null);
  const { data, error } = useAsync<{ items: CalItem[] }>(
    () => call("calendar.month", { year: ym.y, month: ym.m + 1 }), [ym.y, ym.m]);
  const day = useAsync<{ items: CalItem[] } | undefined>(
    () => sel ? call("calendar.day", { date: sel }) : Promise.resolve(undefined), [sel]);

  if (error) return <Err e={error} />;
  const byDate: Record<string, CalItem[]> = {};
  for (const it of data?.items ?? []) {
    (byDate[it.date_start] ??= []).push(it);
  }
  const first = new Date(ym.y, ym.m, 1);
  const days = new Date(ym.y, ym.m + 1, 0).getDate();
  const lead = (first.getDay() + 6) % 7;
  const cells: (number | null)[] = [
    ...Array(lead).fill(null), ...Array.from({ length: days }, (_, i) => i + 1)];

  return (
    <div>
      <div className="row">
        <button onClick={() => setYm(ym.m === 0 ? { y: ym.y - 1, m: 11 } : { y: ym.y, m: ym.m - 1 })}>←</button>
        <strong>{ym.y}-{String(ym.m + 1).padStart(2, "0")}</strong>
        <button onClick={() => setYm(ym.m === 11 ? { y: ym.y + 1, m: 0 } : { y: ym.y, m: ym.m + 1 })}>→</button>
      </div>
      <table className="cal">
        <thead><tr>{["一", "二", "三", "四", "五", "六", "日"].map((d) => <th key={d}>{d}</th>)}</tr></thead>
        <tbody>
          <tr>{cells.map((c, i) => {
            if (c === null) return <td key={i} className="other" />;
            const key = `${ym.y}-${String(ym.m + 1).padStart(2, "0")}-${String(c).padStart(2, "0")}`;
            const n = byDate[key]?.length ?? 0;
            return (
              <td key={i}
                  className={`${n ? "has " : ""}${sel === key ? "selected" : ""}`}
                  onClick={() => setSel(key)}>
                {c}{n ? <div className="cnt">{n}</div> : null}
              </td>
            );
          })}</tr>
        </tbody>
      </table>
      {sel ? (
        day.error ? <Err e={day.error} /> : day.data ? (
          day.data.items.length ? day.data.items.map((it) => (
            <Item key={it.item_id}>
              <Meta><Tag kind={it.kind}>{it.kind}</Tag><span>{it.status}</span></Meta>
              <div className="detail"><strong>{it.title}</strong></div>
              {it.kind === "memory" ? (
                <Meta>preview_kind: <code>{it.preview_kind}</code>
                  {it.preview_kind === "forgotten_summary" ? "（遗忘桶：仅摘要）" : ""}</Meta>
              ) : null}
            </Item>
          )) : <Empty>{sel}：无条目</Empty>
        ) : <Empty>加载中…</Empty>
      ) : <Empty>点击日期查看当天条目</Empty>}
    </div>
  );
}

type Plan = {
  plan_id: string; title: string; state: string; version: number;
  starts_at: string | null; due_at: string | null; date_start: string | null;
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

export function Quotes({ note }: { note: (s: string, err?: boolean) => void }) {
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  const { data, error, reload } = useAsync<{ quotes: Quote[] }>(
    () => call("memory.quotes.list", {}), []);
  const [searchOut, setSearchOut] = useState<{ hits: { text: string; quote_id: string; matched_by: string }[] } | null>(null);

  const keep = async () => {
    if (!text.trim()) return;
    try {
      await call("memory.quotes.keep", { text, said_at: new Date().toISOString() });
      setText(""); note("已保留");
      reload();
    } catch (e) { note(String(e), true); }
  };
  const search = async () => {
    try {
      setSearchOut(await call("memory.quotes.search", { query: q }));
    } catch (e) { note(String(e), true); }
  };
  const withdraw = async (id: string) => {
    try {
      await call("memory.quotes.withdraw", { quote_id: id });
      note("已撤下（不物理删除）");
      reload();
    } catch (e) { note(String(e), true); }
  };

  return (
    <div>
      <div className="row">
        <input value={text} onChange={(e) => setText(e.target.value)}
               placeholder={getWho() === "jiaming" ? "她说的一句话（允许复述）" : "仅周家明身份可保留"}
               style={{ flex: 1 }} />
        <button className="primary" onClick={keep}>保留</button>
      </div>
      <div className="row">
        <input value={q} onChange={(e) => setQ(e.target.value)} style={{ flex: 1 }}
               placeholder="搜她的话（独立检索 source=quotes）" />
        <button onClick={search}>搜索</button>
      </div>
      {searchOut ? (
        searchOut.hits.length ? searchOut.hits.map((h) => (
          <Item key={h.quote_id}>
            <Meta><span>source=<code>quotes</code></span>
              <span>matched_by=<code>{h.matched_by}</code></span></Meta>
            <div className="detail">{h.text}</div>
          </Item>
        )) : <Empty>无命中</Empty>
      ) : null}
      {error ? <Err e={error} /> : null}
      {data?.quotes.map((qt) => (
        <Item key={qt.id}>
          <Meta><span>{qt.id}</span><span>kept_by={qt.kept_by}</span>
            <Tag kind={qt.semantic_status === "material_conflict" ? "state-rejected" : ""}>
              {qt.semantic_status}
            </Tag></Meta>
          <div className="detail">{qt.text}</div>
          <div className="row">
            <button className="danger" onClick={() => withdraw(qt.id)}>撤下</button>
          </div>
        </Item>
      ))}
    </div>
  );
}

type DelReq = {
  id: string; resource_id: string; resource_kind: string; action: string;
  status: string; human_reason: string; ai_reason: string; local_date: string;
};

export function Deletions({ note }: { note: (s: string, err?: boolean) => void }) {
  const [target, setTarget] = useState("");
  const [reason, setReason] = useState("");
  const [action, setAction] = useState("archive");
  const { data, error, reload } = useAsync<{ requests: DelReq[] }>(
    () => call("memory.deletion.list", {}), []);

  const submit = async () => {
    try {
      const d = await call<{ request_id: string }>("memory.deletion.request", {
        resource_id: target, reason, action });
      setTarget(""); setReason("");
      note(`申请已提交 ${d.request_id}（pending，内容未变）`);
      reload();
    } catch (e) { note(String(e), true); }
  };
  const decide = async (r: DelReq, decision: string) => {
    try {
      await call("memory.deletion.decide", {
        request_id: r.id, decision }, `ui-del-${r.id}-${decision}`);
      note(decision === "approve" ? "已批准并执行" : "已拒绝");
      reload();
    } catch (e) { note(String(e), true); reload(); }
  };
  const withdraw = async (rid: string) => {
    try {
      await call("memory.deletion.withdraw", { resource_id: rid });
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
        <select value={action} onChange={(e) => setAction(e.target.value)}>
          <option value="archive">归档</option>
          <option value="delete">删除</option>
        </select>
        <button className="danger" onClick={submit}>提交申请</button>
      </div>
      <Empty>规则与旧系统一致：理由必填；每日 10 条；每资源 5 次；仅周家明审批；审批前内容不变。</Empty>
      {error ? <Err e={error} /> : null}
      {data?.requests.slice(0, 30).map((r) => (
        <Item key={r.id}>
          <Meta>
            <span>{r.id}</span>
            <Tag kind={`state-${r.status}`}>{r.status}</Tag>
            <span>{r.resource_kind}:{r.resource_id}</span>
            <span>{r.action}</span><span>{r.local_date}</span>
          </Meta>
          <div className="detail">理由：{r.human_reason}
            {r.ai_reason ? <><br />批注：{r.ai_reason}</> : null}</div>
          {r.status === "pending" ? (
            <div className="row">
              <button className="primary" onClick={() => decide(r, "approve")}>批准</button>
              <button className="danger" onClick={() => decide(r, "reject")}>拒绝</button>
              <button onClick={() => withdraw(r.resource_id)}>撤回</button>
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

export function Content({ note }: { note: (s: string, err?: boolean) => void }) {
  const [tab, setTab] = useState<"home" | "self" | "diary">("home");
  const home = useAsync<HomeData>(() => call("home.get", {}), [tab]);
  const selfs = useAsync<{ entries: SelfEntry[] }>(
    () => call("self.list", {}), [tab]);
  const diaries = useAsync<{ entries: DiaryEntry[] }>(
    () => call("diary.list", {}), [tab]);
  const [homeText, setHomeText] = useState<string | null>(null);
  const [selfText, setSelfText] = useState("");
  const [diaryTitle, setDiaryTitle] = useState("");
  const [diaryText, setDiaryText] = useState("");

  const saveHome = async () => {
    try {
      const d = await call<{ version: number }>("home.update", {
        content: homeText ?? home.data?.content ?? "",
        expected_version: home.data?.version ?? 0 });
      note(`Home 已更新 v${d.version}`);
      setHomeText(null); home.reload();
    } catch (e) { note(String(e), true); }
  };
  const writeSelf = async () => {
    if (!selfText.trim()) return;
    try {
      const d = await call<{ review_available_on: string }>("self.write", {
        content: selfText });
      setSelfText("");
      note(`已写入（pending，${d.review_available_on} 起可回看）`);
      selfs.reload();
    } catch (e) { note(String(e), true); }
  };
  const writeDiary = async () => {
    if (!diaryText.trim()) return;
    const today = new Date().toISOString().slice(0, 10);
    try {
      await call("diary.write", {
        title: diaryTitle, content: diaryText,
        covers_from: today, covers_to: today });
      setDiaryTitle(""); setDiaryText("");
      note("日记已保存"); diaries.reload();
    } catch (e) { note(String(e), true); }
  };

  return (
    <div>
      <div className="row">
        {(["home", "self", "diary"] as const).map((t) => (
          <button key={t} className={tab === t ? "primary" : ""}
                  onClick={() => setTab(t)}>
            {t === "home" ? "Home" : t === "self" ? "Self" : "日记"}
          </button>
        ))}
      </div>
      {tab === "home" ? (
        <div>
          {home.error ? <Err e={home.error} /> : (
            <>
              <textarea
                value={homeText ?? home.data?.content ?? ""}
                onChange={(e) => setHomeText(e.target.value)}
                placeholder="共同 Home（版本化正本）" />
              <div className="row">
                <button className="primary" onClick={saveHome}>保存</button>
                <Meta>当前 v{home.data?.version ?? 0}</Meta>
              </div>
            </>
          )}
        </div>
      ) : tab === "self" ? (
        <div>
          <textarea value={selfText} onChange={(e) => setSelfText(e.target.value)}
            placeholder={getWho() === "jiaming" ? "今天的一段自我（写了立即算数，隔日可回看）" : "仅周家明可写 Self"} />
          <div className="row">
            <button className="primary" onClick={writeSelf}>写入</button>
          </div>
          {selfs.error ? <Err e={selfs.error} /> : null}
          {selfs.data?.entries.map((s) => (
            <Item key={s.self_id}>
              <Meta>
                <span>{s.self_id}</span>
                <Tag kind={`state-${s.review_state === "pending" ? "submitted" : s.review_state}`}>
                  {s.review_state}
                </Tag>
                <span>{s.written_at.slice(0, 10)}</span>
                <span>{s.review_available_on} 起可回看</span>
              </Meta>
              <div className="detail">{s.content}</div>
            </Item>
          ))}
        </div>
      ) : (
        <div>
          <input value={diaryTitle} onChange={(e) => setDiaryTitle(e.target.value)}
                 placeholder="日记标题" />
          <textarea value={diaryText} onChange={(e) => setDiaryText(e.target.value)}
                    placeholder="正文（全文保留，可独立检索）" />
          <div className="row">
            <button className="primary" onClick={writeDiary}>保存日记</button>
          </div>
          {diaries.error ? <Err e={diaries.error} /> : null}
          {diaries.data?.entries.map((d) => (
            <Item key={d.diary_id}>
              <Meta>
                <span>{d.author}</span>
                <span>v{d.version}</span>
                <span>{d.covers_from ?? ""}{d.covers_to ? ` → ${d.covers_to}` : ""}</span>
              </Meta>
              <div className="detail"><strong>{d.title}</strong><br />{d.content}</div>
            </Item>
          ))}
        </div>
      )}
    </div>
  );
}

type MediaObj = { content_hash: string; mime: string; size: number;
                  owned_by: string; created_at: string };

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
