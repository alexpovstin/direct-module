"use strict";

const view = document.querySelector("#view");
const notice = document.querySelector("#notice");
const operations = {
  list_organizations: "Список организаций",
  list_accounts: "Список рекламных аккаунтов",
  check_connection: "Проверка подключения",
  discover_accounts: "Поиск и проверка аккаунтов",
  get_balance: "Чтение баланса",
  get_spending: "Чтение расходов",
  get_financial_summary: "Финансовая сводка",
  save_organization: "Сохранение организации",
  add_contact: "Добавление контакта",
  edit_contact: "Изменение контакта",
  import_sheet: "Импорт таблицы",
  add_connection: "Добавление подключения",
  rename_connection: "Переименование подключения",
  replace_token: "Замена токена",
  register_account: "Подключение аккаунта",
  save_account: "Сохранение аккаунта",
  grant_permission: "Выдача разрешения",
  revoke_permission: "Отзыв разрешения",
  interpret_text: "Разбор команды",
};
const connectionTypes = {
  own: "Прямой рекламодатель", agency: "Агентство", manager: "Управляющий доступ",
  client: "Клиент агентства (например, eLama)",
};
const yandexTypes = {
  CLIENT: "Прямой рекламодатель", SUBCLIENT: "Клиент агентства", AGENCY: "Агентство",
  UNKNOWN: "Тип не указан",
};
const principalLabels = {local: "Эта веб-панель", interface: "Внешняя интеграция (API)"};
const connectionStatus = {
  unchecked: "Не проверено", authorized: "Авторизовано", error: "Ошибка подключения",
};
const vatOptions = [
  ["unknown", "Неизвестно"], ["excluded", "Без НДС"], ["included", "С НДС"],
];
const forecastReasons = {
  insufficient_data: "Пока недостаточно данных.",
  currency_mismatch: "Валюты баланса и расходов различаются.",
  vat_unconfirmed: "Подтвердите учёт НДС для баланса в настройках аккаунта.",
  nonpositive_spending: "Среднесуточный расход должен быть больше нуля.",
};
const globalOps = ["list_organizations", "list_accounts"];
const connectionOps = ["check_connection", "discover_accounts"];
const financialOps = ["get_balance", "get_spending", "get_financial_summary"];
const discoveries = new Map();
const liveSummaries = new Map();
const edits = {organization: null, account: null};
let permissionAccount = null;
let selectedContact = null;
let navigation = 0;
let fieldSequence = 0;

function node(tag, options = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(options)) {
    if (value != null) element.setAttribute(key, value);
  }
  element.append(...children.flat().filter(child => child != null));
  return element;
}

function notify(message, error = false) {
  notice.hidden = !message;
  notice.className = error ? "notice error" : "notice";
  notice.textContent = message;
}

async function request(path, method = "GET", body) {
  const headers = {"X-Direct-Module": "local-admin"};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch("/api/v1/admin" + path, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(diagnostic(result.error) || "Запрос завершился с ошибкой.");
  }
  return result;
}

function diagnostic(info) {
  if (!info) return "";
  const code = info.yandex_code == null ? "" : " Код Яндекса " + info.yandex_code + ".";
  const retry = info.retry_after_seconds == null ? "" : " Через " + info.retry_after_seconds + " с.";
  return info.message + code + retry;
}

const run = body => request("/commands", "POST", body);

async function action(control, task) {
  control.disabled = true;
  notify("");
  try {
    await task();
  } catch (error) {
    notify(error.message, true);
  } finally {
    control.disabled = false;
  }
}

function button(label, task, primary = false) {
  const control = node("button", {type: "button", class: primary ? "primary" : null}, label);
  control.addEventListener("click", () => action(control, task));
  return control;
}

function form(label, fields, submit) {
  const control = node("button", {class: "primary", type: "submit"}, label);
  const element = node("form", {class: "form", method: "post"}, fields,
    node("div", {class: "actions"}, control));
  element.addEventListener("submit", event => {
    event.preventDefault();
    action(control, () => submit(new FormData(element)));
  });
  return element;
}

function input(name, value = "", options = {}) {
  return node("input", {name, value, ...options});
}

function select(name, values, current) {
  const element = node("select", {name});
  for (const [value, label] of values) element.append(node("option", {value}, label));
  if (current != null) element.value = current;
  return element;
}

function field(label, control, hint) {
  const id = "field-" + ++fieldSequence;
  control.id = id;
  if (hint) control.setAttribute("aria-describedby", id + "-hint");
  return node("div", {class: "field"}, node("label", {for: id}, label), control,
    hint ? node("small", {id: id + "-hint"}, hint) : null);
}

function displayDate(value) {
  return value ? new Intl.DateTimeFormat("ru", {
    dateStyle: "short", timeStyle: "short",
  }).format(new Date(value)) : "—";
}

function number(value) {
  return value == null ? "—" : new Intl.NumberFormat("ru", {
    maximumFractionDigits: 2,
  }).format(Number(value));
}

function amount(value, currency) {
  return value == null ? "—" : number(value) + " " + currency;
}

function table(columns, rows, empty) {
  if (!rows.length) return node("p", {class: "empty"}, empty);
  return node("div", {class: "table-wrap"}, node("table", {},
    node("thead", {}, node("tr", {}, columns.map(label => node("th", {scope: "col"}, label)))),
    node("tbody", {}, rows.map(row => node("tr", {}, row.map(cell => node("td", {}, cell)))))));
}

function panel(title, ...children) {
  return node("section", {class: "panel"}, title ? node("h2", {}, title) : null, children);
}

function page(title, ...children) {
  return [node("h1", {}, title), ...children.flat()];
}

function organizationChoices(data, empty = "Без привязки к организации") {
  return [["", empty], ...data.organizations.map(item => [item.id, item.name])];
}

function labelFor(items, id, getter = item => item.name) {
  const item = items.find(entry => entry.id === id);
  return item ? getter(item) : "—";
}

function badge(value, label) {
  return node("span", {class: "badge " + value}, label);
}

async function grantSet(target, keys) {
  await writeGrants("local", keys, target, true);
  liveSummaries.clear();
  await render();
  notify("Доступ из этой веб-панели разрешён.");
}

async function writeGrants(principal, keys, target, allowed) {
  const principals = principal === "both" ? Object.keys(principalLabels) : [principal];
  const permissions = principals.flatMap(source => keys.map(operation => ({
    principal: source, operation, target_id: target, allowed,
  })));
  await request("/permissions/batch", "PUT", {permissions});
}

function hasGrants(data, id, keys) {
  return keys.every(operation => data.permissions.some(item =>
    item.principal === "local" && item.operation === operation && item.target_id === id));
}

async function refreshSummary(id = null) {
  const body = {operation: "get_financial_summary"};
  if (id != null) body.account_id = id;
  const result = await run(body);
  for (const row of result.data.accounts) liveSummaries.set(row.account.id, row);
  await render();
  const rows = result.data.accounts;
  if (!rows.length) notify("Нет аккаунтов для обновления.");
  else if (rows.some(row => Object.keys(row.diagnostics).length)) {
    notify("Некоторые показатели недоступны. Причины указаны в таблице.", true);
  } else notify("Сводка обновлена.");
}

function overview(data) {
  const rows = data.accounts.map(cached => {
    const live = liveSummaries.get(cached.account.id);
    const item = live?.date_to === cached.date_to ? live : cached;
    const balance = item.balance;
    const spending = item.spending;
    const balanceIssue = diagnostic(item.diagnostics.balance);
    const spendingIssue = diagnostic(item.diagnostics.spending);
    const forecastIssue = item.forecast_reason === "insufficient_data"
      ? (!balance ? "Нет данных о балансе." : "Нет полных данных о расходах.")
      : forecastReasons[item.forecast_reason];
    const forecast = item.forecast_days == null ? node("span", {
      title: forecastIssue,
    }, "—") : number(item.forecast_days);
    const monetaryNote = node("small", {}, [
      balance ? "Баланс · " + vatOptions.find(([key]) => key === balance.vat)[1] : "",
      spending ? "Расход · " + vatOptions.find(([key]) => key === spending.vat)[1] : "",
    ].filter(Boolean).join(" / "));
    return [
      node("div", {}, node("strong", {}, item.organization_name || "Без организации"),
        node("small", {}, item.account.login)),
      node("div", {}, amount(balance?.amount, balance?.currency), monetaryNote,
        balanceIssue ? node("small", {class: "diagnostics"}, balanceIssue) : null),
      item.currency || "—",
      amount(item.spending_yesterday, spending?.currency),
      node("div", {}, amount(item.spending_7_days, spending?.currency),
        spendingIssue ? node("small", {class: "diagnostics"}, spendingIssue) : null),
      amount(item.average_daily_spending, spending?.currency),
      node("div", {}, forecast, item.forecast_reason
        ? node("small", {}, forecastIssue)
        : Number(item.forecast_days) < 0 ? node("small", {}, "Баланс исчерпан.") : null),
      node("div", {}, displayDate(item.updated_at),
        balance && spending && balance.updated_at !== spending.updated_at
          ? node("small", {}, "Баланс " + displayDate(balance.updated_at) +
            " · расходы " + displayDate(spending.updated_at)) : null),
      node("div", {}, badge(item.connection_status, connectionStatus[item.connection_status]),
        item.connection_diagnostic ? node("small", {class: "diagnostics"},
          diagnostic(item.connection_diagnostic)) : null,
        button("Обновить", () => refreshSummary(item.account.id))),
    ];
  });
  const period = data.accounts[0];
  return page("Финансовая сводка", panel(null,
    node("div", {class: "bar"}, node("p", {}, period ?
      "Период расходов " + period.date_from + " — " + period.date_to + " по Москве."
      : "Подключите рекламный аккаунт, чтобы посмотреть баланс и расходы."),
    rows.length ? button("Обновить сводку", () => refreshSummary(), true)
      : node("a", {href: "#connections"}, "К подключениям")),
    table(["Организация и аккаунт", "Баланс", "Валюта", "Вчера", "За 7 дней",
      "В среднем за день", "Дней до исчерпания", "Обновлено", "Подключение"], rows,
      "Подключённых аккаунтов пока нет.")));
}

function organizations(data) {
  const current = data.organizations.find(row => row.id === edits.organization);
  const options = data.accounts.map(item => {
    const option = node("option", {
      value: item.id, selected: current && item.organization_id === current.id ? "" : null,
    }, item.login + (item.organization_id && item.organization_id !== current?.id
      ? " · " + labelFor(data.organizations, item.organization_id) : ""));
    return option;
  });
  const accounts = node("select", {name: "accounts", multiple: "", size: 4}, options);
  const editor = form(current ? "Сохранить изменения" : "Добавить организацию", [
    field("Название", input("name", current?.name, {required: "", maxlength: 200})),
    field("Псевдонимы", node("textarea", {name: "aliases", rows: 3}, current?.aliases.join("\n") || ""),
      "По одному псевдониму в строке."),
    field("Сайт", input("website", current?.website, {type: "url", placeholder: "https://example.ru"})),
    field("Примечание", node("textarea", {name: "notes", rows: 3, maxlength: 4000}, current?.notes || ""),
      "Для рабочих сведений. Доступы из таблицы хранятся в зашифрованном архиве."),
    field("Рекламные аккаунты", accounts, "Выбор аккаунта переносит его в эту организацию."),
  ], async values => {
    await request("/organizations" + (current ? "/" + current.id : ""), current ? "PUT" : "POST", {
      name: values.get("name"), website: values.get("website") || null,
      notes: values.get("notes").trim() || null,
      aliases: values.get("aliases").split(/\r?\n/).map(line => line.trim()).filter(Boolean),
      account_ids: values.getAll("accounts").map(Number),
    });
    edits.organization = null;
    liveSummaries.clear();
    await render();
    notify("Организация сохранена.");
  });
  return page("Организации", node("p", {},
    "Клиенты и проекты, которые вы ведёте в Direct Module. Организация может объединять несколько рекламных аккаунтов."),
    panel(null, table(["Название", "Псевдонимы", "Сайт", "Аккаунты", ""], data.organizations.map(item => [
      item.name, item.aliases.join(", ") || "—", item.website || "—",
      data.accounts.filter(account => account.organization_id === item.id).map(account => account.login).join(", ") || "—",
      button("Открыть карточку", async () => {
        edits.organization = item.id;
        selectedContact = null;
        await render();
        document.querySelector("#organization-card")?.scrollIntoView();
      }),
    ]), "Организаций пока нет.")),
    node("div", {id: "organization-card"}, panel(current ? current.name : "Новая организация", editor,
      current ? button("Закрыть карточку", async () => { edits.organization = null; await render(); }) : null),
      current ? organizationDetails(data, current) : null));
}

function contactForm(organization, contact) {
  return form(contact ? "Сохранить контакт" : "Добавить человека", [
    field("Имя", input("name", contact?.name, {required: "", maxlength: 200})),
    field("Телефон", input("phone", contact?.phone, {type: "tel", maxlength: 80})),
  ], async values => {
    const endpoint = "/organizations/" + organization.id + "/contacts";
    await request(contact ? endpoint + "/" + contact.id : endpoint, contact ? "PUT" : "POST", {
      name: values.get("name"), phone: values.get("phone").trim() || null,
    });
    selectedContact = null;
    await render();
    notify("Контакт сохранён.");
  });
}

function organizationDetails(data, organization) {
  const contacts = data.contacts.filter(item => item.organization_id === organization.id);
  const refs = data.import_references.filter(item => item.organization_id === organization.id);
  return node("div", {},
    panel("Люди", table(["Имя", "Телефон", ""], contacts.map(item => [item.name, item.phone || "—",
      button("Изменить", async () => { selectedContact = item.id; await render(); })]), "Контактов пока нет."),
      contactForm(organization, contacts.find(item => item.id === selectedContact))),
    panel("Доступ к данным", ...data.accounts.filter(item => item.organization_id === organization.id).map(item =>
      button("Настроить права для " + item.login, async () => {
        permissionAccount = item.id;
        location.hash = "permissions";
      })), node("p", {}, "Права действуют на конкретный рекламный аккаунт.")),
    refs.length ? panel("Строки исходной таблицы", node("p", {},
      "Полная копия зашифрована. Номера строк помогают найти исходные сведения при восстановлении архива."),
      table(["Архив", "Вкладка по порядку", "Строка"], refs.map(item =>
        [String(item.archive_id), String(item.sheet), String(item.row)]), "")) : null);
}

function people(data) {
  return page("Люди", panel(null, table(["Имя", "Телефон", "Организация"], data.contacts.map(item => [
    item.name, item.phone || "—", button(labelFor(data.organizations, item.organization_id), async () => {
      edits.organization = item.organization_id;
      selectedContact = item.id;
      location.hash = "organizations";
    }),
  ]), "Контактов пока нет. Добавьте человека в карточке организации."),
  node("a", {href: "#organizations"}, "К организациям")));
}

function accounts(data) {
  const current = data.accounts.find(row => row.id === edits.account);
  const organizationField = selected => field("Организация в Direct Module",
    select("organization", organizationChoices(data), selected),
    "Список берётся из раздела «Организации». Привязку можно добавить позже.");
  const createOrganization = !data.organizations.length
    ? node("a", {href: "#organizations"}, "Добавить организацию") : null;
  const linkForm = current ? null : form("Проверить и добавить", [
    field("Подключение", select("connection", data.connections.map(item => [item.id, item.name]))),
    field("Логин в Яндекс Директе", input("login", "", {maxlength: 255}),
      "Оставьте поле пустым, чтобы добавить аккаунт, на который выдан токен этого подключения."),
    organizationField(),
  ], async values => {
    await request("/accounts", "POST", {
      connection_id: Number(values.get("connection")), login: values.get("login") || null,
      organization_id: Number(values.get("organization")) || null,
    });
    await render();
    notify("Аккаунт добавлен.");
  });
  const editor = current ? form("Сохранить настройки", [
    organizationField(current.organization_id),
    node("details", {}, node("summary", {}, "Настройки общего счёта"),
      field("ID общего счёта", input("shared", current.shared_account_id, {type: "number", min: 1, step: 1}),
        "Номер счёта в Директе. Укажите его, если Яндекс вернул несколько счетов."),
      field("НДС в сумме баланса", select("vat", vatOptions, current.balance_vat),
        "НДС — налог на добавленную стоимость. Для прогноза баланс и расходы должны учитывать налог одинаково. При сомнении выберите «Неизвестно».")),
  ], async values => {
    await request("/accounts/" + current.id, "PUT", {
      organization_id: Number(values.get("organization")) || null,
      shared_account_id: Number(values.get("shared")) || null,
      balance_vat: values.get("vat"),
    });
    liveSummaries.delete(current.id);
    edits.account = null;
    await render();
    notify("Настройки сохранены.");
  }) : null;
  return page("Рекламные аккаунты", panel(null,
    table(["Логин", "Организация", "Подключение", "Валюта", "Действия"],
      data.accounts.map(item => [
        node("strong", {}, item.login), labelFor(data.organizations, item.organization_id),
        labelFor(data.connections, item.connection_id), item.currency || "—",
        node("div", {class: "actions"},
          button("Настройки", async () => { edits.account = item.id; await render(); }),
          hasGrants(data, item.id, financialOps) ? null
            : button("Разрешить баланс и расходы в панели", () => grantSet(item.id, financialOps))),
      ]), "Рекламных аккаунтов пока нет.")),
    current ? panel(current.login, editor, createOrganization,
      button("Отмена", async () => { edits.account = null; await render(); }))
      : panel("Добавить аккаунт", data.connections.length ? node("div", {}, linkForm, createOrganization)
        : node("a", {href: "#connections"}, "Создать подключение")));
}

function tokenField() {
  return field("OAuth-токен", input("oauth_token", "", {
    type: "password", required: "", maxlength: 4096, autocomplete: "off",
    spellcheck: "false", autocapitalize: "off",
  }));
}

function connections(data) {
  const entries = data.connections.map(item => {
    const detected = item.identity?.kind;
    const agency = detected === "AGENCY" || (!detected && item.kind === "agency");
    const managed = item.kind === "manager" && (!detected || detected === "CLIENT");
    const result = discoveries.get(item.id);
    const actions = node("div", {class: "actions"},
      hasGrants(data, item.id, connectionOps) ? null
        : button("Разрешить проверку и поиск", () => grantSet(item.id, connectionOps)),
      button("Проверить авторизацию", async () => {
        try {
          await run({operation: "check_connection", connection_id: item.id});
          notify("Авторизация подтверждена.");
        } finally {
          liveSummaries.clear();
          await render();
        }
      }),
      button(agency ? "Получить список клиентов" : "Показать аккаунт токена", async () => {
        const response = await run({operation: "discover_accounts", connection_id: item.id});
        discoveries.set(item.id, response.data);
        await render();
      }));
    return panel(item.name,
      node("p", {class: "metadata"},
        "Выбранный сценарий — " + connectionTypes[item.kind],
        badge(item.status, connectionStatus[item.status]),
        item.identity ? "Яндекс подтвердил " + item.identity.login + " · " + yandexTypes[detected] : null,
        item.checked_at ? displayDate(item.checked_at) : null),
      item.diagnostic ? node("p", {class: "diagnostics"}, diagnostic(item.diagnostic)) : null,
      actions,
      agency || managed ? node("details", {}, node("summary", {}, "Проверить доступ к другому аккаунту"),
        form("Проверить в Яндексе", [field("Логин другого аккаунта в Директе",
          input("lookup", "", {maxlength: 255, required: ""}),
          "Запрос проверяет, может ли токен читать этот аккаунт в Яндексе. При успехе аккаунт появится ниже, и его можно добавить.")],
        async values => {
          const response = await run({operation: "discover_accounts", connection_id: item.id,
            login: values.get("lookup").trim()});
          discoveries.set(item.id, response.data);
          await render();
        })) : null,
      result ? node("div", {},
        result.diagnostic ? node("p", {}, diagnostic(result.diagnostic)) : null,
        table(["Доступный логин", "Валюта", ""], result.accounts.map(account => [
          account.login, account.currency || "—",
          data.accounts.some(saved => saved.connection_id === item.id
            && saved.yandex_client_id === account.client_id) ? "Добавлен"
            : button("Добавить аккаунт", async () => {
              await request("/accounts", "POST", {connection_id: item.id, login: account.login});
              await render();
              notify("Аккаунт добавлен. Организацию можно указать в его настройках.");
            }),
        ]), "Яндекс не вернул доступных аккаунтов.")) : null,
      node("details", {}, node("summary", {}, "Переименовать подключение"),
        form("Сохранить название", [field("Название подключения",
          input("name", item.name, {required: "", maxlength: 200}))], async values => {
          await request("/connections/" + item.id, "PUT", {name: values.get("name")});
          await render();
          notify("Название подключения изменено.");
        })),
      node("details", {}, node("summary", {}, "Заменить токен"), form("Сохранить новый токен",
        [tokenField()], async values => {
          await request("/connections/" + item.id + "/token", "PUT", {oauth_token: values.get("oauth_token")});
          discoveries.delete(item.id);
          liveSummaries.clear();
          await render();
          notify("Токен заменён. Проверьте авторизацию.");
        })));
  });
  return page("Подключения", entries, panel("Новое подключение",
    form("Сохранить подключение", [
      field("Название", input("name", "", {required: "", maxlength: 200})),
      field("Сценарий подключения", select("kind", Object.entries(connectionTypes), "own"),
        "Выберите, как получили доступ. Проверка авторизации покажет тип аккаунта по данным Яндекса."),
      tokenField(),
    ], async values => {
      await request("/connections", "POST", Object.fromEntries(values));
      await render();
      notify("Подключение сохранено.");
    })));
}

function permissions(data) {
  const scopes = {
    catalog: {operations: globalOps, targets: []},
    connection: {operations: connectionOps, targets: data.connections.map(item => [item.id, item.name])},
    account: {operations: financialOps, targets: data.accounts.map(item => [item.id,
      labelFor(data.organizations, item.organization_id, org => org.name) + " · " + item.login])},
  };
  const scope = select("scope", [["catalog", "Весь каталог"], ["connection", "Одно подключение"],
    ["account", "Один рекламный аккаунт"]]);
  const target = select("target_id", []);
  const targetField = field("Выберите запись", target);
  const principal = select("principal", [...Object.entries(principalLabels), ["both", "Оба источника"]]);
  const checklist = node("fieldset", {class: "checklist"});
  function loadScope() {
    const config = scopes[scope.value];
    targetField.hidden = scope.value === "catalog";
    target.required = !targetField.hidden;
    target.replaceChildren(node("option", {value: ""}, config.targets.length
      ? "Выберите запись" : "Доступных записей пока нет"),
      ...config.targets.map(([id, label]) => node("option", {value: id}, label)));
    checklist.replaceChildren(node("legend", {}, "Операции чтения"), ...config.operations.map(key =>
      node("label", {}, input("operation", key, {type: "checkbox", checked: ""}), operations[key])));
  }
  scope.addEventListener("change", loadScope);
  if (permissionAccount != null) scope.value = "account";
  loadScope();
  if (permissionAccount != null) {
    target.value = String(permissionAccount);
    permissionAccount = null;
  }
  return page("Разрешения",
    panel("Выдать доступ", form("Разрешить выбранные операции", [
      field("Откуда можно запускать команды", principal,
        "Веб-панель — этот сайт, включая консоль. Внешняя интеграция — вызовы по API из проекта «Интерфейс», например для ботов. Для API нужен отдельный ключ."),
      field("К каким данным", scope, "Каталог — списки организаций и аккаунтов. Баланс и отчёты выдаются отдельно по аккаунтам."),
      targetField, checklist,
    ], async values => {
      const selected = values.getAll("operation");
      if (!selected.length) throw new Error("Выберите хотя бы одну операцию.");
      await writeGrants(values.get("principal"), selected, Number(values.get("target_id")) || null, true);
      liveSummaries.clear();
      await render();
      notify("Разрешения выданы.");
    })),
    panel("Выданные разрешения", table(["Источник команд", "Операция", "Область", ""], data.permissions.map(item => [
      principalLabels[item.principal],
      operations[item.operation],
      item.target_id == null ? "Весь каталог" : connectionOps.includes(item.operation)
        ? labelFor(data.connections, item.target_id) : labelFor(data.accounts, item.target_id, account => account.login),
      button("Отозвать", async () => {
        await request("/permissions", "PUT", {...item, allowed: false});
        liveSummaries.clear();
        await render();
        notify("Разрешение отозвано.");
      }),
    ]), "Разрешения пока не выдавались.")));
}

function log(data) {
  const statuses = {ok: "Выполнено", partial: "Частично", error: "Ошибка"};
  return page("Журнал операций", panel(null, table(
    ["Время", "Источник", "Операция", "ID цели", "Статус", "Код диагностики"],
    data.entries.map(item => [
      displayDate(item.observed_at), principalLabels[item.principal],
      operations[item.operation] || item.operation, String(item.target_id ?? "—"),
      badge(item.status, statuses[item.status]), item.code || "—",
    ]), "Операций пока нет.")));
}

function consoleResult(result) {
  const {data} = result;
  if (data.connection) {
    const item = data.connection;
    return node("p", {}, item.name + " · " + connectionStatus[item.status]);
  }
  if (Array.isArray(data.organizations)) {
    return table(["Организация"], data.organizations.map(item => [item.name]), "Организаций пока нет.");
  }
  if (!Array.isArray(data.accounts)) return node("p", {}, "Команда выполнена.");
  if (result.operation === "list_accounts" || result.operation === "discover_accounts") {
    return table(["Логин", "Валюта"], data.accounts.map(item => [item.login, item.currency || "—"]),
      "Аккаунтов нет.");
  }
  return table(["Аккаунт", "Баланс", "Расход за период", "Дней до исчерпания", "Диагностика"],
    data.accounts.map(item => [
      [item.organization_name, item.account.login].filter(Boolean).join(" · "),
      amount(item.balance?.amount, item.balance?.currency),
      amount(item.spending?.total, item.spending?.currency),
      number(item.forecast_days),
      Object.values(item.diagnostics).map(diagnostic).join(" ") || "—",
    ]), "Нет аккаунтов, доступных для этой команды.");
}

function commandConsole(data) {
  const output = node("div", {"aria-live": "polite", hidden: ""});
  const command = node("textarea", {name: "command", rows: 4, maxlength: 1000, required: "",
    placeholder: "Покажи расходы за неделю"});
  const mode = select("mode", [["text", "Текст"], ["structured", "JSON"]]);
  const organization = data.organizations[0]?.name;
  const examples = node("div", {class: "actions"}, [
    organization ? "Проверь баланс организации " + organization : "Сколько денег осталось у всех клиентов?",
    organization ? "Покажи расходы за неделю организации " + organization : "Покажи расходы за неделю",
    "Покажи расходы за вчера", "На сколько дней хватит рекламного бюджета?",
  ].map(example => button(example, async () => {
    mode.value = "text";
    command.value = example;
    command.focus();
  })));
  return page("Консоль команд", panel(null,
    node("p", {}, "Проверьте, какую команду выполнит Direct Module. Выберите пример, затем нажмите «Выполнить». Консоль использует разрешения этой веб-панели."),
    form("Выполнить", [field("Формат", mode, "Текст подходит для обычных запросов. JSON нужен для проверки запросов интеграции."),
      field("Команда", command)], async values => {
      output.hidden = true;
      let body;
      const structured = values.get("mode") === "structured";
      if (structured) {
        try { body = JSON.parse(values.get("command")); }
        catch { throw new Error("Проверьте синтаксис JSON."); }
      } else {
        body = {text: values.get("command")};
      }
      const result = await request(structured ? "/commands" : "/commands/text", "POST", body);
      output.replaceChildren(node("h2", {}, operations[result.operation]), consoleResult(result),
        node("details", {}, node("summary", {}, "Ответ API в JSON"),
          node("pre", {}, JSON.stringify(result, null, 2))));
      output.hidden = false;
      if (result.operation === "get_financial_summary") {
        for (const row of result.data.accounts) liveSummaries.set(row.account.id, row);
      } else if (result.operation !== "list_organizations" && result.operation !== "list_accounts") {
        liveSummaries.clear();
      }
    }), examples, output));
}

const views = {
  overview: ["/dashboard", overview],
  organizations: ["/catalog", organizations],
  people: ["/catalog", people],
  accounts: ["/catalog", accounts],
  connections: ["/catalog", connections],
  permissions: ["/catalog", permissions],
  log: ["/log", log],
  console: ["/catalog", commandConsole],
};

async function render() {
  const revision = ++navigation;
  const requested = location.hash.slice(1);
  const key = Object.hasOwn(views, requested) ? requested : "overview";
  const [url, draw] = views[key];
  for (const link of document.querySelectorAll("nav a")) {
    if (link.hash === "#" + key) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  view.setAttribute("aria-busy", "true");
  view.inert = true;
  try {
    const data = url ? await request(url) : undefined;
    if (revision === navigation) view.replaceChildren(...draw(data));
  } catch (error) {
    if (revision === navigation) notify(error.message, true);
  } finally {
    if (revision === navigation) {
      view.setAttribute("aria-busy", "false");
      view.inert = false;
    }
  }
}

window.addEventListener("hashchange", () => {
  view.replaceChildren();
  notify("");
  render();
});
render();
