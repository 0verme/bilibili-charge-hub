const $ = (s) => document.querySelector(s),
	$$ = (s) => [...document.querySelectorAll(s)];
const widgets = window.DashboardWidgets;
let me,
	page = 1,
	total = 0,
	pageSize = 20,
	dashboardTimezone;
function navigateSameOrigin(path) {
	const target = new URL(path, window.location.origin);
	if (target.origin !== window.location.origin)
		throw new Error("不允许跳转到外部地址");
	window.location.replace(target.href);
}
const jobNames = {
	charge_collection: "充电记录采集",
	coupon_claim: "B 币券领取",
	notification_retry: "通知失败重试",
	daily_task: "每日任务",
};
const jobName = (kind) => jobNames[kind] || kind;
const csrf = () =>
	document.cookie
		.split("; ")
		.find((x) => x.startsWith("csrf_token="))
		?.split("=")
		.slice(1)
		.join("=") || "";
async function api(path, options = {}) {
	const headers = {
		...(options.body ? { "Content-Type": "application/json" } : {}),
		...(options.headers || {}),
	};
	if (options.method && !["GET", "HEAD"].includes(options.method))
		headers["X-CSRF-Token"] = decodeURIComponent(csrf());
	const r = await fetch(path, { ...options, headers });
	if (!r.ok) {
		const d = await r.json().catch(() => ({}));
		const detail = d.detail && typeof d.detail === "object" ? d.detail : d;
		const code = detail.code || "request_failed";
		const message =
			detail.message ||
			(typeof d.detail === "string" ? d.detail : `请求失败 (${r.status})`);
		const error = new Error(message);
		error.code = code;
		error.status = r.status;
		if (r.status === 401 && ["session_expired", "auth_required"].includes(code))
			navigateSameOrigin("/login");
		throw error;
	}
	return r.status === 204 ? null : r.json();
}
function el(tag, text, cls) {
	const n = document.createElement(tag);
	if (text !== undefined) n.textContent = String(text);
	if (cls) n.className = cls;
	return n;
}
function toast(text, error = false) {
	const n = el("div", text, "toast " + (error ? "error" : "success"));
	$("#toast").append(n);
	setTimeout(() => n.remove(), 3500);
}
function button(text, fn, cls = "secondary") {
	const b = el("button", text, cls);
	b.type = "button";
	b.onclick = () => Promise.resolve(fn()).catch((e) => toast(e.message, true));
	return b;
}
function values(form) {
	const d = Object.fromEntries(new FormData(form));
	return Object.fromEntries(Object.entries(d).filter(([, v]) => v !== ""));
}
function formatTime(value) {
	return widgets.formatTime(value, dashboardTimezone);
}
function showFormError(form, message = "") {
	let node = form.querySelector("[data-form-error]");
	if (!node) {
		node = el("p", undefined, "error");
		node.dataset.formError = "true";
		form.append(node);
	}
	node.textContent = message;
	node.classList.toggle("hidden", !message);
}
widgets.initTheme();
$$("[data-view]").forEach(
	(b) =>
		(b.onclick = () => {
			$$("[data-view]").forEach((x) => x.classList.toggle("active", x === b));
			$$(".view").forEach((x) =>
				x.classList.toggle("active", x.id === b.dataset.view),
			);
			$("#title").textContent = b.textContent;
			if (b.dataset.view !== "overview") $("#state").textContent = "";
			loadView(b.dataset.view);
		}),
);
async function loadView(v) {
	if (v === "overview") return loadDashboard();
	if (v === "accounts") return loadAccounts();
	if (v === "jobs") return loadJobs();
	if (v === "notifications") return loadChannels();
	if (v === "shares") return loadShares();
	if (v === "users" && me.role === "admin") return loadUsers();
}
async function loadDashboard() {
	const q = new URLSearchParams(values($("#filters")));
	q.set("page", page);
	q.set("page_size", pageSize);
	const d = await api("/api/dashboard?" + q);
	dashboardTimezone = d.timezone;
	total = d.pagination.total;
	const totalPages = Math.max(1, Math.ceil(total / pageSize));
	if (page > totalPages) {
		page = totalPages;
		return loadDashboard();
	}
	const first = d.trend[0]?.date,
		last = d.trend.at(-1)?.date;
	$("#state").textContent = first
		? `${first} 至 ${last} · ${dashboardTimezone}`
		: `暂无充电记录 · ${dashboardTimezone}`;
	widgets.renderSummary($("#summary"), d);
	widgets.drawTrend($("#trend-chart"), d.trend);
	widgets.renderRanking($("#supporter-ranking"), d.top_supporters);
	widgets.renderMonthly($("#monthly-bars"), d.trend);
	$("#record-count").textContent =
		`共 ${total.toLocaleString("zh-CN")} 条 · 当前第 ${page} 页 · 未脱敏`;
	$("#records").replaceChildren(
		...d.records.map((item) => {
			const tr = el("tr");
			const cells = [
				[formatTime(item.charged_at), ""],
				[item.name, ""],
				[item.uid, ""],
				[widgets.money(item.amount), "money-cell charge"],
				[widgets.money(item.brokerage), "money-cell"],
				[item.remark || "—", "remark-cell"],
			];
			cells.forEach(([value, cls]) => tr.append(el("td", value, cls)));
			return tr;
		}),
	);
	$("#page").textContent = `第 ${page} / ${totalPages} 页`;
	$("#prev").disabled = page <= 1;
	$("#next").disabled = page >= totalPages;
	const select = $("#filters select");
	const current = select.value;
	select.replaceChildren(el("option", "全部账号"));
	select.firstChild.value = "";
	d.accounts.forEach((a) => {
		const o = el("option", a.name || a.uid);
		o.value = a.id;
		select.append(o);
	});
	select.value = current;
}
$("#filters").onsubmit = (e) => {
	e.preventDefault();
	page = 1;
	loadDashboard().catch((x) => toast(x.message, true));
};
$("#prev").onclick = () => {
	if (page > 1) {
		page--;
		loadDashboard().catch((x) => toast(x.message, true));
	}
};
$("#next").onclick = () => {
	if (page * pageSize < total) {
		page++;
		loadDashboard().catch((x) => toast(x.message, true));
	}
};
$("#page-size").onchange = (e) => {
	pageSize = Number(e.target.value);
	page = 1;
	loadDashboard().catch((x) => toast(x.message, true));
};
$("#export").onclick = () => {
	const q = new URLSearchParams(values($("#filters")));
	navigateSameOrigin("/api/dashboard/export.csv?" + q);
};
async function loadAccounts() {
	const items = await api("/api/bili/accounts");
	$("#account-list").replaceChildren(
		...items.map((a) => {
			const c = el("article", undefined, "card");
			c.append(
				el("h3", a.display_name || `UID ${a.bili_uid}`),
				el("p", a.status, "badge"),
				el(
					"p",
					a.last_checked_at
						? `上次检查：${formatTime(a.last_checked_at)}`
						: "尚未采集",
					"muted",
				),
				button("立即采集", async () => {
					const r = await api(`/api/bili/accounts/${a.id}/collect`, {
						method: "POST",
					});
					toast(`采集完成，新增 ${r.inserted} 条`);
					loadDashboard();
				}),
				button("每日任务", async () => {
					await openDailyTask(a);
				}),
				button("修改名称", async () => {
					const name = prompt("显示名称", a.display_name || "");
					if (name)
						await api(`/api/bili/accounts/${a.id}`, {
							method: "PATCH",
							body: JSON.stringify({ display_name: name }),
						});
					loadAccounts();
				}),
				button(
					"解绑",
					async () => {
						if (confirm("解绑将删除该账号及其充电记录，确定继续？")) {
							await api(`/api/bili/accounts/${a.id}`, { method: "DELETE" });
							loadAccounts();
						}
					},
					"danger",
				),
			);
			return c;
		}),
	);
}
$("#bind").onclick = async () => {
	const q = await api("/api/bili/qr-sessions", { method: "POST" });
	const box = $("#qr");
	box.classList.remove("hidden");
	box.querySelector("img").src = `/api/bili/qr-sessions/${q.id}/image`;
	box.querySelector("p").textContent = "等待扫码…";
	const timer = setInterval(async () => {
		try {
			const s = await api(`/api/bili/qr-sessions/${q.id}`);
			box.querySelector("p").textContent = s.status;
			if (s.status === "completed" || s.status === "expired") {
				clearInterval(timer);
				if (s.status === "completed") {
					toast("绑定成功");
					loadAccounts();
				}
			}
		} catch (e) {
			clearInterval(timer);
			toast(e.message, true);
		}
	}, 2000);
};
function jobAccountLabel(job) {
	return job.account
		? `${job.account.display_name || "未命名账号"} · UID ${job.account.bili_uid}`
		: "当前租户（通知中心）";
}
function renderJobCard(j) {
	const c = el("article", undefined, "card");
	c.append(
		el("h3", jobName(j.kind)),
		el("p", j.enabled ? "已启用" : "已停用", "badge"),
		el(
			"p",
			j.next_run_at ? `下次：${formatTime(j.next_run_at)}` : "无下次运行",
			"muted",
		),
		button(j.enabled ? "停用" : "启用", async () => {
			await api(`/api/jobs/${j.id}/enabled?enabled=${!j.enabled}`, {
				method: "PATCH",
			});
			loadJobs();
		}),
		button("修改周期", async () => {
			const seconds = prompt(
				"输入运行间隔（秒，至少 20）",
				j.trigger_config.seconds || 300,
			);
			if (seconds) {
				await api(`/api/jobs/${j.id}/schedule`, {
					method: "PATCH",
					body: JSON.stringify({ interval_seconds: Number(seconds) }),
				});
				loadJobs();
			}
		}),
		button("立即运行", async () => {
			const r = await api(`/api/jobs/${j.id}/run`, { method: "POST" });
			toast(`任务已入队 ${r.run_id}`);
			setTimeout(loadJobs, 1000);
		}),
		button(
			"删除",
			async () => {
				if (confirm("确定删除任务？")) {
					await api(`/api/jobs/${j.id}`, { method: "DELETE" });
					loadJobs();
				}
			},
			"danger",
		),
	);
	return c;
}
function renderJobGroup(title, jobs) {
	const section = el("section", undefined, "job-group");
	const heading = el("div", undefined, "job-group-heading");
	heading.append(el("h2", title), el("span", `${jobs.length} 个任务`, "muted"));
	section.append(heading);
	const grid = el("div", undefined, "grid");
	grid.append(...jobs.map(renderJobCard));
	section.append(grid);
	return section;
}
function runStatusLabel(status) {
	return (
		{
			succeeded: "成功",
			failed: "失败",
			skipped: "跳过 / 无操作",
			partial_success: "部分成功",
			queued: "排队中",
			running: "运行中",
		}[status] || status
	);
}
function runTriggerLabel(trigger) {
	return (
		{
			scheduled: "定时",
			manual: "人工",
			retry: "重试",
			reconciliation: "对账 / 恢复",
		}[trigger] || trigger
	);
}
function runAccountLabel(run) {
	return run.account
		? `${run.account.display_name || "未命名账号"} · UID ${run.account.bili_uid}`
		: "系统任务 / Global";
}
function runSummary(run) {
	if (run.result?.conclusion) return run.result.conclusion;
	return (
		Object.entries(run.result || {})
			.filter(([k]) => !["conclusion", "no_op"].includes(k))
			.map(([k, v]) => `${k}: ${v}`)
			.join(" · ") || "暂无结果数据"
	);
}
async function openRunDetail(runId) {
	const run = await api(`/api/job-runs/${runId}`);
	const body = $("#run-detail-body");
	body.replaceChildren();
	const fields = {
		任务: run.task_name || run.task_key,
		账号: runAccountLabel(run),
		"Execution ID": run.id,
		"Scheduler Job ID": run.schedule_job_id || "-",
		触发: runTriggerLabel(run.trigger_type),
		计划时间: run.scheduled_at ? formatTime(run.scheduled_at) : "-",
		开始: formatTime(run.started_at),
		结束: run.finished_at ? formatTime(run.finished_at) : "-",
		耗时: `${run.duration_ms ?? "-"} ms`,
		状态: runStatusLabel(run.status),
		结论: runSummary(run),
		错误类型: run.error_type || "-",
		错误信息: run.error || "-",
	};
	Object.entries(fields).forEach(([key, value]) => {
		const p = el("p");
		p.append(el("strong", `${key}：`), el("span", value));
		body.append(p);
	});
	const details = el("details"),
		summary = el("summary", "结构化结果 / 关联信息");
	details.append(summary, el("pre", JSON.stringify(run.result || {}, null, 2)));
	body.append(details);
	$("#run-detail").showModal();
}
async function loadJobs() {
	const filter = $("#run-filters");
	const params = filter
		? new URLSearchParams(values(filter))
		: new URLSearchParams();
	if (filter?.changed_only?.checked) params.set("changed_only", "true");
	const [jobs, runs, accounts] = await Promise.all([
		api("/api/jobs"),
		api(`/api/job-runs?${params}`),
		api("/api/bili/accounts"),
	]);
	const accountSelect = filter?.account_id;
	if (accountSelect && accountSelect.options.length === 1)
		accounts.forEach((a) => {
			const option = el(
				"option",
				`${a.display_name || "未命名账号"} · UID ${a.bili_uid}`,
			);
			option.value = a.id;
			accountSelect.append(option);
		});
	const groups = new Map();
	jobs.forEach((j) => {
		const key = j.account?.id || "tenant";
		if (!groups.has(key)) groups.set(key, []);
		groups.get(key).push(j);
	});
	$("#job-list").replaceChildren(
		...[...groups].map(([key, items]) =>
			renderJobGroup(
				key === "tenant" ? "租户级任务 · 通知中心" : jobAccountLabel(items[0]),
				items,
			),
		),
	);
	$("#run-list").replaceChildren(
		...runs.map((r) => {
			const tr = el("tr");
			tr.onclick = () =>
				openRunDetail(r.id).catch((e) => toast(e.message, true));
			[
				runStatusLabel(r.status),
				runAccountLabel(r),
				r.task_name || r.task_key || "系统任务",
				runTriggerLabel(r.trigger_type),
				formatTime(r.started_at),
				`${r.duration_ms ?? "-"} ms`,
				runSummary(r),
			].forEach((v) => tr.append(el("td", v)));
			return tr;
		}),
	);
}
$("#run-filters")?.addEventListener("submit", (event) => {
	event.preventDefault();
	loadJobs().catch((e) => toast(e.message, true));
});
$(".dialog-close")?.addEventListener("click", () => $("#run-detail").close());
const notificationEventLabels = {
	new_charge: "新充电",
	collection_failed: "采集失败",
	cookie_expired: "Cookie 已失效",
	coupon_claim_succeeded: "优惠券领取成功",
	coupon_claim_failed: "优惠券领取失败",
	scheduled_job_failed: "定时任务失败",
	daily_task_succeeded: "每日任务成功",
	daily_task_failed: "每日任务失败",
};
const notificationEventDescriptions = {
	new_charge: "收到新的充电记录",
	collection_failed: "充电记录采集异常",
	cookie_expired: "B 站登录状态需要重新绑定",
	coupon_claim_succeeded: "每月 B 币券领取成功",
	coupon_claim_failed: "每月 B 币券领取失败",
	scheduled_job_failed: "定时任务执行异常",
	daily_task_succeeded: "每日任务有新的完成结果",
	daily_task_failed: "每日任务执行失败或未完成",
};
const notificationProviderLabels = {
	feishu: "飞书",
	telegram: "Telegram",
	serverchan: "Server酱",
	webhook: "Webhook",
};
const notificationStatusLabels = {
	succeeded: "成功",
	failed: "失败",
	pending: "等待发送",
	retrying: "等待重试",
	merged: "已合并",
	unknown: "未知状态",
};
let notificationCatalog = null;
let notificationChannels = [];
let notificationEditingChannel = null;
let notificationRuleDraft = new Map();
let notificationActiveTab = "channels";
function notificationEvents() {
	return notificationCatalog?.events || Object.keys(notificationEventLabels).map((type) => ({
		type,
		label: notificationEventLabels[type],
		description: notificationEventDescriptions[type] || "通知事件",
	}));
}
function notificationEventLabel(type) {
	return notificationEvents().find((item) => item.type === type)?.label || type || "未知事件";
}
function notificationProviderLabel(provider) {
	return (
		notificationCatalog?.providers?.find((item) => item.id === provider)?.name ||
		notificationProviderLabels[provider] ||
		provider ||
		"渠道已删除"
	);
}
function notificationStatusLabel(delivery) {
	return (
		delivery?.status_label ||
		notificationStatusLabels[delivery?.display_status] ||
		notificationStatusLabels[delivery?.status] ||
		"未知状态"
	);
}
function setNotificationState(id, kind, message, retry) {
	const node = $("#" + id);
	if (!node) return;
	node.className = `notification-state ${kind}`;
	node.replaceChildren(el("p", message));
	if (retry) node.append(button("重新加载", retry, "secondary"));
	node.classList.toggle("hidden", kind === "success");
}
function hideNotificationState(id) {
	const node = $("#" + id);
	if (node) node.className = "notification-state hidden";
}
function showNotificationDialog(dialog) {
	if (dialog?.showModal) dialog.showModal();
}
function closeNotificationDialog(dialog) {
	if (dialog?.open) dialog.close();
}
function notificationProvider(providerId) {
	return notificationCatalog?.providers?.find((item) => item.id === providerId);
}
function renderNotificationProviders() {
	const list = $("#notification-provider-list");
	const providers = notificationCatalog?.providers || [];
	if (!list) return;
	list.replaceChildren(
		...providers.map((provider) => {
			const card = el("article", undefined, "notification-provider-card");
			const heading = el("div", undefined, "notification-provider-heading");
			heading.append(
				el("span", provider.icon, "notification-provider-icon"),
				el("div", undefined, "notification-provider-copy"),
			);
			heading.lastChild.append(
				el("h3", provider.name),
				el("p", provider.description, "muted"),
			);
			card.append(heading, button("＋ 配置", () => openChannelDialog(provider.id)));
			return card;
		}),
	);
	if (providers.length) hideNotificationState("notification-provider-state");
	else setNotificationState("notification-provider-state", "empty", "当前没有可用的通知方式");
}
function replaceNotificationSelect(select, firstLabel, items, valueOf, labelOf) {
	if (!select) return;
	const current = select.value;
	select.replaceChildren(el("option", firstLabel));
	select.firstChild.value = "";
	items.forEach((item) => {
		const option = el("option", labelOf(item));
		option.value = valueOf(item);
		select.append(option);
	});
	select.value = items.some((item) => String(valueOf(item)) === current) ? current : "";
}
function renderChannelFields(providerId, currentConfig = {}) {
	const container = $("#channel-fields");
	const provider = notificationProvider(providerId);
	if (!container || !provider) return;
	const keepSecrets =
		notificationEditingChannel && notificationEditingChannel.provider === providerId;
	container.replaceChildren(
		...provider.fields.map((field) => {
			const label = el("label", undefined, "field notification-config-field");
			label.append(el("span", `${field.label}${field.required ? " *" : ""}`));
			let input;
			if (field.type === "select") {
				input = el("select");
				(field.options || []).forEach((optionValue) => input.append(el("option", optionValue)));
			} else if (field.type === "json") {
				input = el("textarea");
				input.rows = 3;
			} else {
				input = el("input");
				input.type = field.type === "password" ? "password" : field.type;
			}
			input.name = `config.${field.key}`;
			input.placeholder =
				keepSecrets && field.secret ? "已保存，留空保持不变" : field.placeholder || "";
			input.required = Boolean(field.required && !(keepSecrets && field.secret));
			const value = currentConfig[field.key];
			if (value !== undefined && !field.secret && value !== "***") {
				input.value = field.type === "json" ? JSON.stringify(value, null, 2) : value;
			}
			label.append(input);
			if (field.help) label.append(el("small", field.help, "muted"));
			return label;
		}),
	);
}
function openChannelDialog(providerId, channel = null) {
	if (!notificationCatalog) return;
	notificationEditingChannel = channel;
	const form = $("#channel-form");
	const providerSelect = form.elements.provider;
	replaceNotificationSelect(
		providerSelect,
		"选择通知方式",
		notificationCatalog.providers,
		(item) => item.id,
		(item) => item.name,
	);
	providerSelect.value = providerId;
	form.elements.name.value = channel?.name || "";
	$("#channel-dialog-title").textContent = channel ? "编辑通知渠道" : "配置通知渠道";
	showFormError(form);
	renderChannelFields(providerId, channel?.config_masked || {});
	showNotificationDialog($("#channel-dialog"));
}
function collectChannelConfig(form) {
	const provider = notificationProvider(form.elements.provider.value);
	const config = {};
	for (const field of provider?.fields || []) {
		const input = form.elements[`config.${field.key}`];
		const raw = String(input?.value || "").trim();
		if (!raw) continue;
		if (field.type === "json") {
			try {
				config[field.key] = JSON.parse(raw);
			} catch {
				throw new Error(`${field.label}必须是有效的 JSON`);
			}
		} else {
			config[field.key] = raw;
		}
	}
	return config;
}
function notificationActionButton(text, handler, cls = "secondary") {
	const action = el("button", text, cls);
	action.type = "button";
	action.onclick = async () => {
		const original = action.textContent;
		action.disabled = true;
		action.textContent = "发送中…";
		try {
			await handler();
		} catch (error) {
			toast(error.message, true);
		} finally {
			action.disabled = false;
			action.textContent = original;
		}
	};
	return action;
}

$("#daily-task-cancel").onclick = () =>
	$("#daily-task-panel").classList.add("hidden");
let dailyTaskAccountId = null;
function fillDailyTaskForm(p) {
	const f = $("#daily-task-form");
	f.enabled.checked = p.enabled;
	f.target_coins.value = p.target_coins;
	f.protected_coins.value = p.protected_coins;
	f.select_like.checked = p.select_like;
	f.skip_when_lv6.checked = p.skip_when_lv6;
	f.share_enabled.checked = p.share_enabled;
	f.watch_enabled.checked = p.watch_enabled;
	f.support_up_ids.value = (p.support_up_ids || []).join(", ");
}
function collectDailyTaskForm() {
	const f = $("#daily-task-form"),
		d = Object.fromEntries(new FormData(f));
	return {
		enabled: f.enabled.checked,
		target_coins: Number(d.target_coins || 0),
		protected_coins: Number(d.protected_coins || 0),
		select_like: f.select_like.checked,
		skip_when_lv6: f.skip_when_lv6.checked,
		share_enabled: f.share_enabled.checked,
		watch_enabled: f.watch_enabled.checked,
		support_up_ids: String(d.support_up_ids || "")
			.split(/[,，\s]+/)
			.map((s) => Number(s))
			.filter((n) => Number.isFinite(n) && n > 0),
	};
}
async function openDailyTask(a) {
	dailyTaskAccountId = a.id;
	const p = await api(`/api/bili/accounts/${a.id}/daily-task`);
	fillDailyTaskForm(p);
	$("#daily-task-panel").classList.remove("hidden");
	$("#daily-task-panel").scrollIntoView({ behavior: "smooth" });
	await loadDailyTaskRecords(a.id);
}
async function loadDailyTaskRecords(accountId) {
	const records = await api(
		`/api/bili/accounts/${accountId}/daily-task-records`,
	);
	$("#daily-task-records").replaceChildren(
		...records.map((r) => {
			const tr = el("tr");
			[
				r.task_date,
				r.status,
				`${r.coins_donated}/${r.target_coins}`,
				r.share_done ? "√" : "—",
				r.watch_done ? "√" : "—",
				r.message || "-",
			].forEach((v) => tr.append(el("td", v)));
			return tr;
		}),
	);
}
$("#daily-task-form").onsubmit = async (e) => {
	e.preventDefault();
	if (!dailyTaskAccountId) return;
	if (
		confirm("保存后将按新配置执行每日任务；投币会消耗硬币，请确认配置无误。")
	) {
		await api(`/api/bili/accounts/${dailyTaskAccountId}/daily-task`, {
			method: "PUT",
			body: JSON.stringify(collectDailyTaskForm()),
		});
		toast("每日任务配置已保存");
		await loadDailyTaskRecords(dailyTaskAccountId);
		loadJobs();
	}
};
function renderNotificationChannels() {
	const list = $("#channel-list");
	if (!list) return;
	if (!notificationChannels.length) {
		list.replaceChildren();
		setNotificationState(
			"notification-channel-state",
			"empty",
			"尚未配置通知渠道。配置一个渠道后，系统才能发送充电、Cookie 和任务通知。",
		);
		$("#notification-channel-state").append(
			button("配置第一个渠道", () => openChannelDialog(notificationCatalog?.providers?.[0]?.id)),
		);
		return;
	}
	hideNotificationState("notification-channel-state");
	list.replaceChildren(
		...notificationChannels.map((channel) => {
			const card = el("article", undefined, "notification-channel-card");
			const top = el("div", undefined, "notification-channel-top");
			const title = el("div", undefined, "notification-channel-title");
			title.append(
				el("h3", channel.name),
				el("p", notificationProviderLabel(channel.provider), "muted"),
			);
			const toggleLabel = el("label", undefined, "notification-switch");
			const toggle = el("input");
			toggle.type = "checkbox";
			toggle.checked = channel.enabled;
			toggle.setAttribute("aria-label", `${channel.name} ${channel.enabled ? "已启用" : "已停用"}`);
			toggle.onchange = async () => {
				const next = toggle.checked;
				toggle.disabled = true;
				try {
					await api(`/api/notifications/channels/${channel.id}/enabled?enabled=${next}`, {
						method: "PATCH",
					});
					toast(next ? "渠道已启用" : "渠道已停用");
					await loadNotificationChannels();
				} catch (error) {
					toggle.checked = !next;
					toast(error.message, true);
				} finally {
					toggle.disabled = false;
				}
			};
			toggleLabel.append(toggle, el("span", channel.enabled ? "启用" : "停用"));
			top.append(title, toggleLabel);
			const events = channel.event_types || [];
			const summary = events.length
				? events.map(notificationEventLabel).join(" · ")
				: "暂未订阅通知事件";
			const eventLine = el("p", summary, events.length ? "notification-event-summary" : "muted");
			const actions = el("div", undefined, "notification-card-actions");
			actions.append(
				notificationActionButton("测试", async () => {
					const result = await api(`/api/notifications/channels/${channel.id}/test`, {
						method: "POST",
					});
					if (!result.success) {
						toast(`测试失败：${result.detail || result.status_label}`, true);
						return;
					}
					toast(`测试发送成功：${result.detail || "已送达"}`);
				}),
				button("编辑", () => openChannelDialog(channel.provider, channel)),
				button(
					"删除",
					async () => {
						if (!confirm(`确认删除通知渠道「${channel.name}」？历史发送记录会保留。`)) return;
						await api(`/api/notifications/channels/${channel.id}`, { method: "DELETE" });
						toast("渠道已删除，历史发送记录已保留");
						await loadNotificationChannels();
						if (notificationActiveTab === "rules") await loadNotificationRules();
					},
					"danger",
				),
			);
			card.append(top, eventLine, actions);
			return card;
		}),
	);
}
async function loadNotificationCatalog() {
	setNotificationState("notification-provider-state", "loading", "正在加载通知方式…");
	try {
		notificationCatalog = await api("/api/notifications/catalog");
		renderNotificationProviders();
	} catch (error) {
		setNotificationState(
			"notification-provider-state",
			"error",
			`通知方式加载失败：${error.message}`,
			loadNotificationCatalog,
		);
		throw error;
	}
}
async function loadNotificationChannels() {
	setNotificationState("notification-channel-state", "loading", "正在加载已配置渠道…");
	try {
		notificationChannels = await api("/api/notifications/channels");
		renderNotificationChannels();
		updateDeliveryFilters();
	} catch (error) {
		setNotificationState(
			"notification-channel-state",
			"error",
			`渠道加载失败：${error.message}`,
			loadNotificationChannels,
		);
		throw error;
	}
}
async function loadChannels() {
	try {
		if (!notificationCatalog) await loadNotificationCatalog();
		await loadNotificationChannels();
	} catch {
		return;
	}
}


function renderNotificationRules(data) {
	const head = $("#notification-rules-head");
	const body = $("#notification-rules-list");
	const channels = data.channels || [];
	notificationChannels = channels;
	notificationRuleDraft = new Map(
		(data.rules || []).map((rule) => [rule.event_type, new Set(rule.channel_ids || [])]),
	);
	head.replaceChildren(el("th", "事件"));
	channels.forEach((channel) => {
		const th = el("th", undefined, "notification-rule-channel");
		th.append(
			el("strong", channel.name),
			el("small", notificationProviderLabel(channel.provider), "muted"),
		);
		if (!channel.enabled) th.append(el("small", "已停用", "muted"));
		head.append(th);
	});
	body.replaceChildren(
		...(data.events || []).map((event) => {
			const tr = el("tr");
			const eventCell = el("td", undefined, "notification-rule-event");
			eventCell.append(el("strong", event.label), el("small", event.description, "muted"));
			tr.append(eventCell);
			channels.forEach((channel) => {
				const td = el("td", undefined, "notification-rule-check");
				const checkbox = el("input");
				checkbox.type = "checkbox";
				checkbox.checked = notificationRuleDraft.get(event.type)?.has(channel.id) || false;
				checkbox.setAttribute("aria-label", `${event.label} → ${channel.name}`);
				checkbox.onchange = () => {
					const selected = notificationRuleDraft.get(event.type) || new Set();
					if (checkbox.checked) selected.add(channel.id);
					else selected.delete(channel.id);
					notificationRuleDraft.set(event.type, selected);
					$("#notification-rules-save").disabled = false;
				};
				td.append(checkbox);
				tr.append(td);
			});
			return tr;
		}),
	);
	$("#notification-rules-save").disabled = true;
	if (channels.length) hideNotificationState("notification-rules-state");
	else setNotificationState("notification-rules-state", "empty", "还没有可配置的通知渠道，请先在“通知渠道”中完成配置。");
}
async function loadNotificationRules() {
	setNotificationState("notification-rules-state", "loading", "正在加载通知规则…");
	try {
		const data = await api("/api/notifications/subscriptions");
		renderNotificationRules(data);
		updateDeliveryFilters();
	} catch (error) {
		setNotificationState(
			"notification-rules-state",
			"error",
			`通知规则加载失败：${error.message}`,
			loadNotificationRules,
		);
	}
}
async function saveNotificationRules() {
	const save = $("#notification-rules-save");
	const original = save.textContent;
	save.disabled = true;
	save.textContent = "保存中…";
	try {
		await api("/api/notifications/subscriptions", {
			method: "PUT",
			body: JSON.stringify({
				rules: notificationEvents().map((event) => ({
					event_type: event.type,
					channel_ids: [...(notificationRuleDraft.get(event.type) || [])],
				})),
			}),
		});
		toast("通知规则已保存");
		await loadNotificationRules();
		await loadNotificationChannels();
	} catch (error) {
		toast(`规则保存失败：${error.message}`, true);
		save.disabled = false;
	} finally {
		save.textContent = original;
	}
}
function updateDeliveryFilters() {
	replaceNotificationSelect(
		$("#delivery-event-filter"),
		"全部事件",
		notificationEvents(),
		(item) => item.type,
		(item) => `${item.label} · ${item.type}`,
	);
	replaceNotificationSelect(
		$("#delivery-channel-filter"),
		"全部渠道",
		notificationChannels,
		(item) => item.id,
		(item) => `${item.name} · ${notificationProviderLabel(item.provider)}`,
	);
}
function renderDeliveryEvent(delivery) {
	const cell = el("span", undefined, "notification-delivery-event");
	cell.append(el("strong", delivery.event_label || notificationEventLabel(delivery.event_type)));
	if (delivery.event_type) cell.append(el("small", delivery.event_type, "muted"));
	if (delivery.event_summary) cell.append(el("small", delivery.event_summary, "muted"));
	return cell;
}
function renderDeliveryAccount(delivery) {
	const cell = el("span", undefined, "notification-delivery-account");
	cell.append(el("span", delivery.account_name || "未关联账号"));
	if (delivery.account_uid) cell.append(el("small", `UID ${delivery.account_uid}`, "muted"));
	return cell;
}
function renderDeliveryChannel(delivery) {
	const cell = el("span", undefined, "notification-delivery-channel");
	cell.append(el("strong", delivery.channel_name || "渠道已删除"));
	cell.append(el("small", delivery.channel_provider_name || "渠道已删除", "muted"));
	return cell;
}
function renderDeliveryStatus(delivery) {
	const key = String(delivery.display_status || delivery.status || "unknown").replace(/[^a-z0-9_-]/gi, "-");
	return el("span", notificationStatusLabel(delivery), `badge notification-delivery-status status-${key}`);
}
function deliveryResult(delivery) {
	return (
		delivery.error_summary ||
		delivery.response_summary ||
		(delivery.display_status === "succeeded" ? "已送达" : "暂无结果摘要")
	);
}
function renderNotificationDeliveries(deliveries) {
	const body = $("#delivery-list");
	body.replaceChildren(
		...deliveries.map((delivery) => {
			const row = el("tr");
			row.className = "notification-delivery-row";
			row.append(
				el("td", formatTime(delivery.activity_at || delivery.notification_created_at)),
				el("td", undefined, "notification-delivery-event-cell"),
				el("td", undefined, "notification-delivery-account-cell"),
				el("td", undefined, "notification-delivery-channel-cell"),
				el("td", undefined, "notification-delivery-status-cell"),
				el("td", `${delivery.attempts} / ${delivery.max_attempts}`),
				el("td", deliveryResult(delivery), "notification-delivery-result"),
				el("td", undefined, "notification-delivery-actions"),
			);
			row.children[1].append(renderDeliveryEvent(delivery));
			row.children[2].append(renderDeliveryAccount(delivery));
			row.children[3].append(renderDeliveryChannel(delivery));
			row.children[4].append(renderDeliveryStatus(delivery));
			row.children[7].append(button("查看详情", () => openDeliveryDetail(delivery.id)));
			if (delivery.can_retry) {
				row.children[7].append(
					button("重试", () => retryNotificationDelivery(delivery), "secondary"),
				);
			}
			return row;
		}),
	);
}
async function loadNotificationDeliveries() {
	setNotificationState("notification-delivery-state", "loading", "正在加载发送记录…");
	updateDeliveryFilters();
	try {
		const form = $("#delivery-filters");
		const params = new URLSearchParams(values(form));
		const deliveries = await api(`/api/notifications/deliveries?${params}`);
		renderNotificationDeliveries(deliveries);
		if (deliveries.length) hideNotificationState("notification-delivery-state");
		else setNotificationState("notification-delivery-state", "empty", "暂无发送记录");
	} catch (error) {
		setNotificationState(
			"notification-delivery-state",
			"error",
			`发送记录加载失败：${error.message}`,
			loadNotificationDeliveries,
		);
	}
}
function appendNotificationDetailField(list, label, value) {
	const term = el("dt", label);
	const description = el("dd", value === undefined || value === null || value === "" ? "—" : value);
	list.append(term, description);
}
function renderNotificationDetail(delivery) {
	const body = $("#delivery-detail-body");
	const overview = el("dl", undefined, "notification-detail-grid");
	appendNotificationDetailField(overview, "事件", delivery.event_label);
	appendNotificationDetailField(overview, "事件摘要", delivery.event_summary);
	appendNotificationDetailField(overview, "账号", delivery.account_name || "未关联账号");
	appendNotificationDetailField(overview, "渠道", delivery.channel_name);
	appendNotificationDetailField(overview, "Provider", delivery.channel_provider_name);
	appendNotificationDetailField(overview, "状态", notificationStatusLabel(delivery));
	appendNotificationDetailField(overview, "通知创建时间", formatTime(delivery.notification_created_at));
	appendNotificationDetailField(overview, "最近活动时间", formatTime(delivery.activity_at));
	appendNotificationDetailField(overview, "投递完成时间", formatTime(delivery.delivered_at));
	appendNotificationDetailField(overview, "下次可尝试时间", formatTime(delivery.available_at));
	appendNotificationDetailField(overview, "尝试次数", `${delivery.attempts} / ${delivery.max_attempts}`);
	appendNotificationDetailField(overview, "响应摘要", delivery.response_summary);
	appendNotificationDetailField(overview, "错误类型", delivery.error_type);
	appendNotificationDetailField(overview, "错误摘要", delivery.error_summary);
	body.replaceChildren(overview);
	if (delivery.can_retry) body.append(button("重试此通知", () => retryNotificationDelivery(delivery)));
	const technical = el("details", undefined, "notification-technical-detail");
	technical.append(el("summary", "技术关联信息"));
	const technicalFields = el("dl", undefined, "notification-detail-grid");
	appendNotificationDetailField(technicalFields, "Event / Outbox ID", delivery.notification_id || delivery.outbox_id);
	appendNotificationDetailField(technicalFields, "Delivery ID", delivery.delivery_id || delivery.id);
	appendNotificationDetailField(technicalFields, "Channel ID", delivery.channel_id);
	appendNotificationDetailField(technicalFields, "合并来源", delivery.merged_into_outbox_id);
	technical.append(technicalFields);
	const payload = el("details");
	payload.append(el("summary", "脱敏事件摘要"), el("pre", JSON.stringify(delivery.payload_summary || {}, null, 2)));
	body.append(technical, payload);
}
async function openDeliveryDetail(deliveryId) {
	const body = $("#delivery-detail-body");
	body.replaceChildren(el("p", "正在加载详情…", "muted"));
	showNotificationDialog($("#delivery-detail"));
	try {
		const delivery = await api(`/api/notifications/deliveries/${deliveryId}`);
		renderNotificationDetail(delivery);
	} catch (error) {
		body.replaceChildren(el("p", `详情加载失败：${error.message}`, "error"));
	}
}
async function retryNotificationDelivery(delivery) {
	if (!delivery.can_retry) return;
	if (!confirm("确认重新发送这条通知？系统会重新计算该事件的投递预算。")) return;
	await api(`/api/notifications/deliveries/${delivery.id}/retry`, { method: "POST" });
	toast("通知已进入等待重试队列");
	closeNotificationDialog($("#delivery-detail"));
	await loadNotificationDeliveries();
}
$("#channel-form").onsubmit = async (event) => {
	event.preventDefault();
	const form = event.target;
	showFormError(form);
	try {
		const config = collectChannelConfig(form);
		const channel = notificationEditingChannel;
		const payload = {
			name: form.elements.name.value.trim(),
			provider: form.elements.provider.value,
			config,
			event_types: channel?.event_types?.length
				? channel.event_types
				: notificationEvents().map((item) => item.type),
			enabled: channel ? channel.enabled : true,
		};
		if (!form.reportValidity()) return;
		const path = channel ? `/api/notifications/channels/${channel.id}` : "/api/notifications/channels";
		const submit = $("#channel-dialog-submit");
		submit.disabled = true;
		submit.textContent = "保存中…";
		await api(path, {
			method: channel ? "PUT" : "POST",
			body: JSON.stringify(payload),
		});
		toast(channel ? "渠道已更新" : "渠道已配置");
		closeNotificationDialog($("#channel-dialog"));
		notificationEditingChannel = null;
		await loadNotificationChannels();
		if (notificationActiveTab === "rules") await loadNotificationRules();
	} catch (error) {
		showFormError(form, error.message);
	} finally {
		const submit = $("#channel-dialog-submit");
		submit.disabled = false;
		submit.textContent = "保存渠道";
	}
};
$("#channel-form").elements.provider.onchange = (event) =>
	renderChannelFields(event.target.value, notificationEditingChannel?.config_masked || {});
$("#channel-dialog-cancel").onclick = () => {
	notificationEditingChannel = null;
	closeNotificationDialog($("#channel-dialog"));
};
$("#channel-dialog-close").onclick = () => {
	notificationEditingChannel = null;
	closeNotificationDialog($("#channel-dialog"));
};
$("#delivery-detail-close").onclick = () => closeNotificationDialog($("#delivery-detail"));
$("#notification-rules-save").onclick = () =>
	saveNotificationRules().catch((error) => toast(error.message, true));
$("#delivery-filters").onsubmit = (event) => {
	event.preventDefault();
	loadNotificationDeliveries();
};
$("#delivery-filter-reset").onclick = () => {
	$("#delivery-filters").reset();
	loadNotificationDeliveries();
};
$$(["channels", "rules", "deliveries"]).forEach((tabName) => {
	const tab = $(`[data-notification-tab="${tabName}"]`);
	if (!tab) return;
	tab.onclick = () => {
		notificationActiveTab = tabName;
		$$(["channels", "rules", "deliveries"]).forEach((name) => {
			const item = $(`[data-notification-tab="${name}"]`);
			const panel = $(`#notification-${name}-panel`);
			const active = name === tabName;
			item?.classList.toggle("active", active);
			item?.setAttribute("aria-selected", String(active));
			panel?.classList.toggle("hidden", !active);
		});
		if (tabName === "rules") loadNotificationRules();
		if (tabName === "deliveries") loadNotificationDeliveries();
	};
});
async function copyText(text) {
	try {
		if (navigator.clipboard?.writeText) {
			await navigator.clipboard.writeText(text);
			return true;
		}
	} catch {}
	const input = el("textarea");
	input.value = text;
	input.style.position = "fixed";
	input.style.opacity = "0";
	document.body.append(input);
	input.select();
	let copied = false;
	try {
		copied = document.execCommand("copy");
	} catch {}
	input.remove();
	if (!copied) throw new Error("无法访问剪贴板，请手动复制");
	return true;
}
function showCreatedShare(url, copied) {
	let panel = $("#created-share");
	if (!panel) {
		panel = el("article", undefined, "panel");
		panel.id = "created-share";
		$("#share-form").parentElement.append(panel);
	}
	const input = el("input");
	input.value = url;
	input.readOnly = true;
	input.setAttribute("aria-label", "新创建的分享链接");
	panel.replaceChildren(
		el(
			"p",
			copied
				? "分享链接已复制；也可在此手动复制："
				: "无法访问剪贴板，请手动复制：",
			"muted",
		),
		input,
		button("复制链接", async () => {
			await copyText(url);
			toast("链接已复制");
		}),
	);
}
$("#share-form").onsubmit = async (e) => {
	e.preventDefault();
	const v = values(e.target);
	v.expires_hours = Number(v.expires_hours);
	v.mask_names = true;
	v.mask_uids = true;
	const r = await api("/api/dashboard/shares", {
		method: "POST",
		body: JSON.stringify(v),
	});
	const url = r.share_url;
	let copied = false;
	try {
		await copyText(url);
		copied = true;
	} catch {}
	showCreatedShare(url, copied);
	toast(copied ? "链接已复制" : "分享已创建，请手动复制链接");
	loadShares();
};
async function loadShares() {
	const items = await api("/api/dashboard/shares");
	$("#share-list").replaceChildren(
		...items.map((s) => {
			const c = el("article", undefined, "card");
			const status = s.active ? "有效" : s.revoked_at ? "已撤销" : "已过期";
			c.append(
				el("h3", s.password_protected ? "密码分享" : "公开分享"),
				el("p", `${status} · 到期：${formatTime(s.expires_at)}`, "muted"),
			);
			if (s.active && s.share_url) {
				c.append(
					button("复制链接", async () => {
						await copyText(s.share_url);
						toast("链接已复制");
					}),
				);
			} else if (s.active && s.legacy) {
				c.append(
					button("重新生成链接", async () => {
						const r = await api(`/api/dashboard/shares/${s.id}/regenerate`, {
							method: "POST",
						});
						await copyText(r.share_url);
						toast("链接已复制");
						loadShares();
					}),
				);
			}
			if (s.active)
				c.append(
					button(
						"撤销",
						async () => {
							await api(`/api/dashboard/shares/${s.id}`, { method: "DELETE" });
							loadShares();
						},
						"danger",
					),
				);
			return c;
		}),
	);
}
$("#password-form").onsubmit = async (e) => {
	e.preventDefault();
	showFormError(e.target);
	try {
		await api("/api/auth/change-password", {
			method: "POST",
			body: JSON.stringify(values(e.target)),
		});
		e.target.reset();
		toast("密码已修改，其他会话已退出");
	} catch (error) {
		showFormError(e.target, error.message);
	}
};
$("#user-form").onsubmit = async (e) => {
	e.preventDefault();
	await api("/api/users", {
		method: "POST",
		body: JSON.stringify(values(e.target)),
	});
	e.target.reset();
	loadUsers();
};
async function loadUsers() {
	const items = await api("/api/users");
	$("#user-list").replaceChildren(
		...items.map((u) => {
			const row = el("div", undefined, "row panel");
			row.append(
				el("span", `${u.username} · ${u.role}`),
				button(
					u.is_active ? "停用" : "启用",
					async () => {
						await api(`/api/users/${u.id}`, {
							method: "PATCH",
							body: JSON.stringify({ is_active: !u.is_active }),
						});
						loadUsers();
					},
					u.is_active ? "danger" : "secondary",
				),
				button("重置密码", async () => {
					const p = prompt("输入新密码");
					if (p) {
						await api(`/api/users/${u.id}/reset-password`, {
							method: "POST",
							body: JSON.stringify({ new_password: p }),
						});
						toast("密码已重置");
					}
				}),
			);
			return row;
		}),
	);
}
$("#logout").onclick = async () => {
	await api("/api/auth/logout", { method: "POST" });
	navigateSameOrigin("/login");
};
(async () => {
	try {
		me = await api("/api/auth/me");
		$("#identity").textContent = `${me.username} · ${me.role}`;
		if (me.role === "admin") $("#admin-users").classList.remove("hidden");
		await loadDashboard();
	} catch (e) {
		if (!["auth_required", "session_expired"].includes(e.code))
			toast(e.message, true);
	}
})();
