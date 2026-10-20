/* Single-browser smoke flows against a synthetic, isolated FastAPI instance. */
const assert = require('node:assert/strict')
const fs = require('node:fs')
const { chromium } = require(process.env.MARIPOSA_PLAYWRIGHT_MODULE || 'playwright')
const base = process.env.MARIPOSA_E2E_URL || 'http://127.0.0.1:18881'
const fixture = JSON.parse(fs.readFileSync(process.env.MARIPOSA_E2E_FIXTURE, 'utf8'))
;(async () => {
  const browser = await chromium.launch({ headless:true, ...(process.env.MARIPOSA_CHROMIUM_EXECUTABLE ? { executablePath: process.env.MARIPOSA_CHROMIUM_EXECUTABLE } : {}) })
  const context = await browser.newContext({ viewport:{width:1280,height:900}, reducedMotion:'reduce' })
  const page = await context.newPage()
  page.setDefaultTimeout(10000)
  const errors=[]; page.on('pageerror', e=>errors.push(String(e)))
  page.on('dialog', d=>d.accept())
  const cap = async (name,args={},principal='j') => {
    const r=await context.request.post(`${base}/api/capability/${name}`,{headers:{Authorization:`Bearer tok-${principal}`},data:{arguments:args}})
    const body=await r.json(); assert.equal(r.status(),200,JSON.stringify(body));return body.data
  }
  const waitCap = name => page.waitForResponse(r=>r.url().endsWith('/api/capability/'+name)&&r.status()===200)
  const login = async principal => {
    await page.getByPlaceholder('登录密码').fill(`browser-${principal}-password`)
    await page.getByRole('button',{name:'进入',exact:true}).click()
    await page.getByRole('button',{name:'退出',exact:true}).waitFor()
  }
  const close = async () => page.getByRole('button',{name:'关闭',exact:true}).first().click()
  const find = async (term,title) => {
    await page.getByRole('button',{name:'搜索',exact:true}).click()
    const ready=waitCap('memory.recall')
    await page.getByPlaceholder('事件关键词，空格分隔多个…').fill(term)
    await ready
    await page.getByRole('heading',{name:title,exact:true}).first().waitFor()
    const confirmed=waitCap('memory.view.confirm')
    await page.getByRole('heading',{name:title,exact:true}).first().click()
    await confirmed
  }
  try {
    await page.goto(base)
    await login('jiaming')
    assert.match(await page.locator('header').innerText(),/31 个记忆桶/)
    const retired = await context.request.post(base+'/api/capability/memory.pin',{headers:{Authorization:'Bearer tok-j'},data:{arguments:{memory_id:fixture.target}}})
    assert.equal(retired.status(),404)
    await page.getByRole('button',{name:'下一页',exact:true}).click()
    const confirmed=waitCap('memory.view.confirm')
    await page.getByRole('heading',{name:'无日期记忆',exact:true}).click()
    await confirmed
    assert.equal((await cap('memory.get',{memory_id:fixture.unknown})).memory_date,null)
    await close()
    console.log('PASS login, nullable-date pagination and explicit-open receipt')

    await find('紫藤','紫藤记忆')
    await page.getByRole('button',{name:'编辑',exact:true}).click()
    const aside=page.locator('aside').first()
    await aside.locator('input:not([type])').first().fill('紫藤修订')
    await aside.locator('textarea').first().fill('紫藤更新后的事件正文。')
    await aside.locator('input[type=date]').fill('')
    // 分类：平行多选 + 剧本/日记独占（她 2026-10-20 终裁）
    await aside.getByRole('button', { name: /日常/ }).click() // 打开分类下拉（当前：日常）
    const tick = (name) => aside.locator('.z-20 button').filter({ hasText: name }).locator('span').textContent()
    await aside.locator('.z-20 button').filter({ hasText: '甜蜜' }).click() // 平行多选：追加
    assert.equal(await tick('日常'), '✓'); assert.equal(await tick('甜蜜'), '✓')
    await aside.locator('.z-20 button').filter({ hasText: '剧本' }).click() // 独占：整组替换为剧本
    assert.equal(await tick('剧本'), '✓'); assert.equal(await tick('甜蜜'), ''); assert.equal(await tick('日常'), '')
    await aside.locator('.z-20 button').filter({ hasText: '做爱' }).click() // 再选普通类：替换掉独占类
    assert.equal(await tick('剧本'), ''); assert.equal(await tick('做爱'), '✓')
    await aside.locator('.fixed.inset-0.z-10').click({ position: { x: 5, y: 5 } }) // 收起下拉
    const saved=waitCap('memory.update'), reconfirmed=waitCap('memory.view.confirm')
    await aside.getByRole('button',{name:'保存修订',exact:true}).click()
    await saved; await reconfirmed
    let current=await cap('memory.get',{memory_id:fixture.target})
    assert.equal(current.original_title,'紫藤修订'); assert.equal(current.memory_date,null);assert.equal(current.version,2)
    assert.deepEqual(current.categories,['sex'])
    console.log('PASS parallel multi-select categories with reloplay/diary exclusivity (edit saved as [sex])')
    await aside.getByRole('button',{name:'编辑心情',exact:true}).click()
    await aside.getByRole('button',{name:/未选择/}).click()
    await aside.getByRole('button',{name:'开心',exact:true}).click()
    // Close the local multiselect without closing the bucket drawer.
    await aside.locator('.fixed.inset-0.z-10').click({position:{x:5,y:5}})
    await aside.locator('textarea').fill('合成心情补标签')
    const mood=waitCap('memory.mood.update')
    await aside.getByRole('button',{name:'保存心情',exact:true}).click();await mood
    current=await cap('memory.get',{memory_id:fixture.target})
    assert.deepEqual(current.mood.tags,['开心']);assert.equal(current.version,2)
    await aside.getByRole('button',{name:/留下回忆/}).click()
    await aside.getByPlaceholder('再次看到时，想说点什么…').fill('浏览器写下的回忆')
    await aside.getByRole('checkbox').check()
    const remembered=waitCap('memory.recollections.append')
    await aside.getByRole('button',{name:'留下',exact:true}).click();await remembered
    await aside.getByText('浏览器写下的回忆',{exact:true}).waitFor()
    await aside.getByRole('button',{name:'修订',exact:true}).click()
    await aside.getByLabel('修订本人回忆').fill('浏览器修订后的回忆')
    const revised=waitCap('memory.recollections.revise')
    await aside.getByRole('button',{name:'保存修订',exact:true}).click();await revised
    await aside.getByText('浏览器修订后的回忆',{exact:true}).waitFor()
    const revoked=waitCap('memory.keep.revoke')
    await aside.getByRole('button',{name:'撤销我的留标记',exact:true}).click();await revoked
    await close()
    console.log('PASS atomic edit, null date, authorized mood tags, recall append/revise and keep revoke')

    await page.getByRole('button',{name:'原文',exact:true}).click()
    await page.getByLabel('原文关键词',{exact:true}).fill('橙花')
    const search=waitCap('source.search')
    await page.getByRole('button',{name:'搜索原文',exact:true}).click();await search
    await page.getByRole('button',{name:'加载更多',exact:true}).waitFor()
    const more=waitCap('source.search')
    await page.getByRole('button',{name:'加载更多',exact:true}).click();await more
    await page.getByText(/已加载 35 条/).waitFor()
    const opened=waitCap('source.conversation.get')
    await page.getByRole('button').filter({hasText:'橙花原文第00条'}).click();await opened
    await page.locator('[data-source-seq]').first().waitFor()
    await page.getByRole('button',{name:'后台',exact:true}).click()
    await page.getByRole('button',{name:'保存',exact:true}).waitFor()
    assert.equal(await page.getByRole('button',{name:'保存',exact:true}).isDisabled(),true)
    assert.equal(await page.getByRole('button',{name:/预热语义/}).isDisabled(),true)
    console.log('PASS source search continuation, exact message navigation, J admin permissions')

    await page.getByRole('button',{name:'退出',exact:true}).click();await login('qiaosheng')
    await find('无日期','无日期记忆')
    await page.getByRole('button',{name:'申请删除这个记忆…',exact:true}).click()
    await page.getByLabel('删除理由',{exact:true}).fill('浏览器合成删除申请')
    const submitted=waitCap('memory.deletion.request')
    await page.getByRole('button',{name:'确定',exact:true}).click();await submitted
    await close()
    await page.getByRole('button',{name:'后台',exact:true}).click()
    await page.getByRole('button',{name:'撤回申请',exact:true}).waitFor()
    const withdrawn=waitCap('memory.deletion.withdraw')
    await page.getByRole('button',{name:'撤回申请',exact:true}).click();await withdrawn
    assert.equal((await cap('memory.deletion.list',{memory_id:fixture.unknown},'q')).requests[0].status,'withdrawn')
    await find('无日期','无日期记忆')
    await page.getByRole('button',{name:'申请删除这个记忆…',exact:true}).click()
    await page.getByLabel('删除理由',{exact:true}).fill('浏览器合成审批')
    const resubmitted=waitCap('memory.deletion.request')
    await page.getByRole('button',{name:'确定',exact:true}).click();await resubmitted
    await close()
    await page.getByRole('button',{name:'退出',exact:true}).click();await login('jiaming')
    await page.getByRole('button',{name:'下一页',exact:true}).click()
    await page.getByRole('heading',{name:'紫藤修订',exact:true}).click()
    await page.getByRole('button',{name:'直删',exact:true}).waitFor({state:'visible'})
    const deleted=waitCap('memory.deletion.direct')
    await page.getByRole('button',{name:'直删',exact:true}).click();await deleted
    await page.waitForFunction(()=>document.querySelector('header')?.textContent.includes('30 个记忆桶'))
    assert.equal(await page.getByText('这个分类还没有记忆',{exact:true}).count(),0)
    assert.equal(await page.locator('main article').count(),30)
    await page.getByRole('button',{name:'后台',exact:true}).click()
    await page.getByRole('button',{name:'批准删除',exact:true}).waitFor()
    assert.equal(await page.getByRole('button',{name:'撤回申请',exact:true}).count(),0)
    const approved=waitCap('memory.deletion.decide')
    await page.getByRole('button',{name:'批准删除',exact:true}).click();await approved
    assert.equal((await cap('memory.deletion.list',{memory_id:fixture.unknown})).requests[0].status,'approved')
    assert.deepEqual(errors,[])
    console.log('PASS request/withdraw/approve, direct delete and last-page normalization; zero browser errors')
    fs.writeFileSync((process.env.MARIPOSA_E2E_ARTIFACTS || '.')+'/result.json', JSON.stringify({passed_flows:5,browser_errors:errors,flows:['login-null-date-open','atomic-edit-mood','category-multiselect-exclusivity','source-pagination-permissions','deletion-lifecycle-last-page']},null,2))
  } catch(e) {
    const dir=process.env.MARIPOSA_E2E_ARTIFACTS || '.'
    await page.screenshot({path:dir+'/failure.png',fullPage:true})
    fs.writeFileSync(dir+'/failure.html',await page.content())
    throw e
  } finally { await context.close();await browser.close() }
})().catch(e=>{console.error(e);process.exitCode=1})
