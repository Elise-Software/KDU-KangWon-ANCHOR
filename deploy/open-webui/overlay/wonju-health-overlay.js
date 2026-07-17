(() => {
  "use strict";

  const BRAND = "원주시 생활건강 안내 AI";
  const META_SELECTOR = ".language-wonju-health-meta, [class~='language-wonju-health-meta']";
  const GUIDANCE_STEPS = new Map([
    ["1. 먼저 마음부터", "empathy"],
    ["2. 생각해볼 수 있는 원인", "cause"],
    ["3. 지금 할 수 있는 대처", "care"],
    ["4. 상비의약품 안내", "medicine"],
    ["5. 가까운 의료기관 찾기", "nearby"]
  ]);
  const QUICK_QUESTIONS = [
    {
      icon: "cross",
      title: "증상과 대처 알아보기",
      description: "불편한 증상을 편하게 적어주세요",
      prompt: "몸이 불편한데 어떻게 대처하면 좋을지 알려주세요."
    },
    {
      icon: "pin",
      title: "가까운 병원·약국 찾기",
      description: "읍면동을 함께 적으면 더 정확해요",
      prompt: "제가 있는 동네에서 이용할 수 있는 병원이나 약국을 찾아주세요."
    },
    {
      icon: "building",
      title: "보건소·상담기관 찾기",
      description: "주소, 전화번호, 이용 방법 안내",
      prompt: "원주시 보건소와 상담기관의 이용 방법을 알려주세요."
    },
    {
      icon: "heart",
      title: "마음이 너무 힘들어요",
      description: "지금 바로 연결할 도움을 안내해요",
      prompt: "마음이 너무 힘들어요. 지금 도움받을 수 있는 곳을 알려주세요."
    }
  ];
  const RISK_COPY = {
    emergency: {
      title: "즉시 도움을 요청하세요",
      body: "생명이 위급할 수 있습니다. 온라인 답변을 기다리지 말고 바로 연락하세요."
    },
    suicide: {
      title: "지금 혼자 있지 마세요",
      body: "즉시 전문 상담 또는 긴급 구조를 요청하세요. 아래 버튼을 누르면 바로 전화할 수 있습니다."
    },
    addiction: {
      title: "과다복용·의식 저하는 즉시 119",
      body: "호흡 이상이나 의식 저하가 있으면 상담보다 응급 구조가 우선입니다."
    },
    medical_high_risk: {
      title: "의료진의 직접 확인이 필요합니다",
      body: "개인별 진단·처방·복용량은 답변으로 정할 수 없습니다. 약사 또는 의료진에게 직접 확인하세요."
    }
  };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function isVisible(node) {
    if (!node) return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && getComputedStyle(node).display !== "none";
  }

  function icon(name, className = "") {
    const wrapper = element("span", `wonju-health-icon ${className}`.trim());
    wrapper.setAttribute("aria-hidden", "true");
    const paths = {
      brand: '<path d="M12 21s-7-4.35-7-10a4 4 0 0 1 7-2.65A4 4 0 0 1 19 11c0 5.65-7 10-7 10Z"/><path d="M9 12h6M12 9v6"/>',
      cross: '<path d="M9.25 3.75h5.5v5.5h5.5v5.5h-5.5v5.5h-5.5v-5.5h-5.5v-5.5h5.5v-5.5Z"/>',
      pin: '<path d="M20 10c0 5-8 11-8 11S4 15 4 10a8 8 0 1 1 16 0Z"/><circle cx="12" cy="10" r="2.5"/>',
      building: '<path d="M4 21V7l8-4 8 4v14M8 10h2m4 0h2M8 14h2m4 0h2M10 21v-3h4v3"/>',
      heart: '<path d="M20.8 5.8a5.5 5.5 0 0 0-7.8 0L12 6.9l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 22l8.8-8.4a5.5 5.5 0 0 0 0-7.8Z"/>',
      phone: '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.4 19.4 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 2 .7 2.9a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.2-1.2a2 2 0 0 1 2.1-.5c.9.3 1.9.6 2.9.7A2 2 0 0 1 22 16.9Z"/>',
      plus: '<path d="M12 5v14M5 12h14"/>',
      history: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/>',
      user: '<circle cx="12" cy="8" r="4"/><path d="M4.5 21a7.5 7.5 0 0 1 15 0"/>',
      menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
      external: '<path d="M14 3h7v7M10 14 21 3M21 14v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h6"/>',
      info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>'
    };
    wrapper.innerHTML = `<svg viewBox="0 0 24 24" focusable="false">${paths[name] || paths.brand}</svg>`;
    return wrapper;
  }

  function serviceLockup(compact = false) {
    const lockup = element("div", compact ? "wonju-health-lockup is-compact" : "wonju-health-lockup");
    const mark = element("span", "wonju-health-brand-mark");
    mark.append(icon("brand"));
    const copy = element("span", "wonju-health-lockup-copy");
    const title = element("strong", "", BRAND);
    title.dataset.mobileLabel = "생활건강 AI";
    copy.append(
      title,
      element("small", "", compact ? "공식자료 기반 안내" : "원주시 보건·복지 공식자료 기반")
    );
    lockup.append(mark, copy);
    return lockup;
  }

  function decodeMetadata(rawText) {
    const text = String(rawText || "");
    const rawCandidates = text.match(/[A-Za-z0-9_-]{40,}={0,2}/g) || [];
    const candidates = rawCandidates.flatMap((candidate) => {
      const jsonStart = candidate.indexOf("eyJ");
      return jsonStart > 0 ? [candidate, candidate.slice(jsonStart)] : [candidate];
    });
    const compact = text.replace(/\s+/g, "");
    const compactStart = compact.indexOf("eyJ");
    if (compactStart >= 0) {
      const joined = compact.slice(compactStart).match(/^[A-Za-z0-9_-]+={0,2}/)?.[0];
      if (joined) candidates.unshift(joined);
    }
    for (const encoded of candidates.sort((left, right) => right.length - left.length)) {
      try {
        const normalized = encoded.replace(/-/g, "+").replace(/_/g, "/");
        const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
        const bytes = Uint8Array.from(atob(padded), (character) => character.charCodeAt(0));
        const decodedText = new TextDecoder("utf-8").decode(bytes);
        let depth = 0;
        let inString = false;
        let escaped = false;
        let objectStart = -1;
        let objectText = "";
        for (let index = 0; index < decodedText.length; index += 1) {
          const character = decodedText[index];
          if (objectStart < 0) {
            if (character !== "{") continue;
            objectStart = index;
            depth = 1;
            continue;
          }
          if (escaped) {
            escaped = false;
            continue;
          }
          if (inString && character === "\\") {
            escaped = true;
            continue;
          }
          if (character === '"') {
            inString = !inString;
            continue;
          }
          if (inString) continue;
          if (character === "{") depth += 1;
          if (character === "}") depth -= 1;
          if (depth === 0) {
            objectText = decodedText.slice(objectStart, index + 1);
            break;
          }
        }
        const metadata = JSON.parse(objectText || decodedText);
        if (metadata.schema_version === "wonju-health-card-v1") return { metadata, encoded };
      } catch (_) {
        // Streaming can leave an incomplete Base64 value. The observer retries
        // after CodeMirror appends the remaining characters.
      }
    }
    return null;
  }

  function safePhone(value) {
    return String(value || "").replace(/[^0-9+*-]/g, "");
  }

  function safeSourceUrl(value) {
    try {
      const url = new URL(value);
      return url.protocol === "https:" || url.protocol === "http:" ? url.href : "";
    } catch (_) {
      return "";
    }
  }

  function safeMapUrl(value) {
    try {
      const url = new URL(value);
      return url.protocol === "https:" && url.hostname === "map.kakao.com" ? url.href : "";
    } catch (_) {
      return "";
    }
  }

  function institutionMapUrl(institution) {
    const explicit = safeMapUrl(institution.map_url);
    if (explicit) return explicit;
    const query = [institution.name, institution.address]
      .map((value) => String(value || "").trim())
      .filter(Boolean)
      .join(" ");
    return query ? `https://map.kakao.com/link/search/${encodeURIComponent(query)}` : "";
  }

  function addDefinition(list, label, value) {
    if (!value) return false;
    const row = element("div", "wonju-health-detail");
    row.append(element("dt", "", label), element("dd", "", value));
    list.append(row);
    return true;
  }

  function safetyCard(metadata) {
    const copy = RISK_COPY[metadata.risk_category];
    if (!copy || !metadata.safety_rule_applied) return null;
    const card = element("section", "wonju-health-card wonju-health-safety-card");
    card.dataset.risk = metadata.risk_category;
    card.setAttribute("role", "alert");
    const heading = element("div", "wonju-health-safety-heading");
    heading.append(icon("heart"), element("h3", "", copy.title));
    card.append(heading, element("p", "", copy.body));
    const actions = element("div", "wonju-health-call-actions");
    for (const [index, contact] of (metadata.safety_contacts || []).entries()) {
      const phone = safePhone(contact.phone);
      if (!phone) continue;
      const priorityClass = index === 0 ? "is-primary" : "is-secondary";
      const link = element("a", `wonju-health-call-button wonju-health-emergency-call ${priorityClass}`, contact.label || `${phone} 전화`);
      link.href = `tel:${phone}`;
      link.setAttribute("aria-label", `${contact.label || phone} 지금 전화하기`);
      link.prepend(icon("phone"));
      actions.append(link);
    }
    if (actions.childElementCount) card.append(actions);
    return card;
  }

  function institutionCards(institutions) {
    if (!institutions || !institutions.length) return null;
    const stack = element("section", "wonju-health-card-stack");
    stack.setAttribute("aria-label", "관련 기관 정보");
    for (const institution of institutions) {
      const card = element("article", "wonju-health-card wonju-health-institution-card");
      const topline = element("div", "wonju-health-card-topline");
      const titleGroup = element("div", "wonju-health-card-title-group");
      titleGroup.append(icon("building"), element("h3", "", institution.name || "관련 기관"));
      topline.append(titleGroup, element("span", "wonju-health-official-badge", "원주시 공식자료"));
      card.append(topline);
      const statusLabel = String(institution.current_status_label || "").trim();
      const statusBasis = String(institution.current_status_basis || "").trim();
      const status = statusLabel === "공식 출처에서 운영 확인"
        ? "공식 자료에서 운영 정보를 확인했습니다."
        : [statusLabel, statusBasis === "수집된 공식 출처" ? "" : statusBasis]
          .filter(Boolean)
          .join(" · ");
      if (status) card.append(element("p", "wonju-health-institution-status", status));
      const details = element("dl");
      addDefinition(details, "주소", institution.address);
      const phones = (institution.phones || []).map((row) => `${row.label}: ${row.value}`).join(" / ");
      addDefinition(details, "전화번호", phones);
      const hours = (institution.operation_hours || []).map((row) => `${row.label} ${row.value}`).join(" / ");
      addDefinition(details, "운영시간", hours);
      if (institution.review_notice) addDefinition(details, "확인 안내", institution.review_notice);
      card.append(details);
      const missing = [];
      if (!institution.address) missing.push("주소");
      if (!phones) missing.push("전화번호");
      if (!hours) missing.push("운영시간");
      if (missing.length) {
        card.append(element("p", "wonju-health-missing-note", `공식 자료에서 확인되지 않은 정보 · ${missing.join(", ")}`));
      }
      const actions = element("div", "wonju-health-call-actions");
      for (const phoneRow of institution.phones || []) {
        const phone = safePhone(phoneRow.value);
        if (!phone) continue;
        const link = element("a", "wonju-health-call-button wonju-health-routine-call", `${phoneRow.label || "전화"} ${phone}`);
        link.href = `tel:${phone}`;
        link.setAttribute("aria-label", `${institution.name || "기관"} ${phoneRow.label || "전화"} ${phone} 전화하기`);
        link.prepend(icon("phone"));
        actions.append(link);
      }
      const mapUrl = institutionMapUrl(institution);
      if (mapUrl) {
        const link = element("a", "wonju-health-call-button wonju-health-map-button", "지도에서 보기");
        link.href = mapUrl;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.setAttribute("aria-label", `${institution.name || "기관"} 카카오맵에서 보기`);
        link.prepend(icon("pin"));
        actions.append(link);
      }
      if (actions.childElementCount) card.append(actions);
      stack.append(card);
    }
    return stack;
  }

  function sourceCards(citations) {
    if (!citations || !citations.length) return null;
    const stack = element("section", "wonju-health-card-stack wonju-health-source-stack");
    stack.setAttribute("aria-label", "답변 출처");
    const heading = element("div", "wonju-health-section-heading");
    heading.append(icon("external"), element("h3", "", "답변에 사용한 공식자료"));
    stack.append(heading);
    for (const citation of citations) {
      const card = element("article", "wonju-health-card wonju-health-source-card");
      const url = safeSourceUrl(citation.url);
      const title = citation.document || citation.doc_id || "공식 문서";
      if (url) {
        const link = element("a");
        link.href = url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.setAttribute("aria-label", `${title} 새 창에서 열기`);
        link.append(element("span", "wonju-health-source-title", title), icon("external"));
        card.append(link);
      } else {
        card.append(element("h4", "", title));
      }
      const technical = element("details", "wonju-health-source-technical");
      technical.append(
        element("summary", "", "근거 식별정보 보기"),
        element("div", "wonju-health-source-meta", `문서 ${citation.doc_id || "-"} · 청크 ${citation.chunk_id || "-"}`)
      );
      card.append(technical);
      stack.append(card);
    }
    return stack;
  }

  function codeBlockShell(marker) {
    if (marker.matches("[data-wonju-metadata-shell]")) return marker;
    const pre = marker.closest("pre");
    if (pre) return pre;
    const languageContainer = marker.closest(".language-wonju-health-meta") || marker;
    return languageContainer.parentElement || languageContainer;
  }

  function liveMetadataShells() {
    const shells = [];
    for (const label of document.querySelectorAll("#messages-container span, #messages-container div")) {
      if ((label.textContent || "").trim() !== "wonju-health-meta") continue;
      let node = label.parentElement;
      for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
        if (node.matches(".chat-assistant, .wonju-health-assistant-message")) break;
        if (!decodeMetadata(node.textContent || "")) continue;
        node.dataset.wonjuMetadataShell = "true";
        shells.push(node);
        break;
      }
    }
    return shells;
  }

  function directChildOf(root, node) {
    let branch = node;
    while (branch && branch.parentElement !== root) branch = branch.parentElement;
    return branch && branch.parentElement === root ? branch : null;
  }

  function answerContent(shell) {
    let node = shell.parentElement;
    while (node && node !== document.body) {
      const hasFallbackHeading = [...node.querySelectorAll("h2, h3")].some((heading) =>
        ["기관 정보", "출처"].includes((heading.textContent || "").trim())
      );
      if (hasFallbackHeading) return node;
      node = node.parentElement;
    }
    return shell.parentElement;
  }

  function removeMarkdownFallback(content, shellBranch, replacedSections) {
    for (const heading of [...content.querySelectorAll("h3")]) {
      if (!replacedSections.includes((heading.textContent || "").trim())) continue;
      let node = directChildOf(content, heading);
      while (node && node !== shellBranch) {
        const next = node.nextSibling;
        const isMetadataBoundary = node.nodeType === Node.ELEMENT_NODE && (
          node.matches("[data-wonju-metadata-shell], .language-wonju-health-meta")
          || node.querySelector("[data-wonju-metadata-shell], .language-wonju-health-meta")
          || (node.textContent || "").includes("wonju-health-meta")
        );
        if (
          node !== directChildOf(content, heading)
          && node.nodeType === Node.ELEMENT_NODE
          && (/^H[1-3]$/.test(node.tagName) || isMetadataBoundary)
        ) break;
        node.remove();
        node = next;
      }
    }
  }

  function renderCards(marker) {
    const decoded = decodeMetadata(marker.textContent || "");
    if (!decoded) return;

    const shell = codeBlockShell(marker);
    const content = answerContent(shell);
    if (!content) return;
    const shellBranch = directChildOf(content, shell) || shell;
    const fingerprint = decoded.encoded.slice(-48);
    if (content.querySelector(`[data-wonju-metadata="${fingerprint}"]`)) {
      shellBranch.remove();
      return;
    }

    const metadata = decoded.metadata;
    const safety = safetyCard(metadata);
    const institutions = institutionCards(metadata.institutions);
    const sources = sourceCards(metadata.citations);
    // Remove the readable markdown fallback only when cards replace it. A
    // no-evidence answer has no cards, so its explicit "출처 없음" notice must
    // remain visible to the resident.
    const replacedSections = [];
    if (institutions) replacedSections.push("기관 정보");
    if (sources) replacedSections.push("출처");
    if (replacedSections.length) removeMarkdownFallback(content, shellBranch, replacedSections);

    if (safety) {
      const safetyHost = element("div", "wonju-health-rendered-cards");
      safetyHost.dataset.wonjuMetadata = fingerprint;
      safetyHost.append(safety);
      content.insertBefore(safetyHost, content.firstChild);
      const repeatedSafetyText = [...content.querySelectorAll("p")].find((paragraph) => {
        if (paragraph.closest(".wonju-health-safety-card")) return false;
        const text = (paragraph.textContent || "").trim();
        return text.length < 280 && /109/.test(text) && /(119|112)/.test(text) && /(즉시|혼자)/.test(text);
      });
      repeatedSafetyText?.classList.add("wonju-health-safety-duplicate");
    }

    const infoHost = element("div", "wonju-health-rendered-cards");
    if (!safety) infoHost.dataset.wonjuMetadata = fingerprint;
    if (institutions) infoHost.append(institutions);
    if (sources) infoHost.append(sources);
    if (infoHost.childElementCount) content.insertBefore(infoHost, shellBranch);
    shellBranch.remove();
  }

  function setQuestionInput(value) {
    const input = document.querySelector("#chat-input, textarea[aria-label], textarea, [contenteditable='true'][role='textbox']");
    if (!input) return;
    input.focus();
    if ("value" in input) {
      input.value = value;
      input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    } else {
      input.textContent = value;
      input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
    }
    input.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function ensureAuthExperience() {
    const authPage = document.querySelector("#auth-page");
    const form = document.querySelector("#auth-container form");
    if (!authPage || !form) return false;

    document.body.classList.add("wonju-health-auth");
    document.body.classList.remove("wonju-health-chat");
    authPage.classList.add("wonju-health-auth-page");
    document.querySelector("#auth-container")?.classList.add("wonju-health-auth-container");
    form.classList.add("wonju-health-auth-form");
    const formCard = form.parentElement;
    formCard?.classList.add("wonju-health-auth-card");

    const passwordInput = form.querySelector("input[type='password'], input[name='password']");
    const passwordShell = passwordInput?.parentElement;
    passwordShell?.classList.add("wonju-health-password-shell");
    passwordInput?.classList.add("wonju-health-password-input");
    passwordShell?.querySelector("button")?.classList.add("wonju-health-password-toggle");

    if (formCard && !formCard.querySelector(".wonju-health-auth-form-brand")) {
      const brand = element("div", "wonju-health-auth-form-brand");
      brand.append(serviceLockup(true), element("p", "", "원주시 생활건강 서비스를 이용하려면 로그인해 주세요."));
      formCard.insertBefore(brand, form);
    }

    const authTitle = [...form.querySelectorAll("h1, h2, h3, div, p, span")]
      .filter((node) => {
        const text = (node.textContent || "").trim();
        return text.includes("Open WebUI") && text.length < 100;
      })
      .sort((left, right) => (left.textContent || "").length - (right.textContent || "").length)[0];
    if (authTitle) {
      authTitle.textContent = "로그인";
      authTitle.classList.add("wonju-health-auth-title");
    }

    if (!authPage.querySelector("#wonju-health-auth-story")) {
      const story = element("aside", "wonju-health-auth-story");
      story.id = "wonju-health-auth-story";
      story.setAttribute("aria-label", "서비스 소개");
      story.append(serviceLockup());
      const copy = element("div", "wonju-health-auth-story-copy");
      copy.append(
        element("span", "wonju-health-kicker", "원주시 생활건강 안내"),
        element("h1", "", "필요한 건강 정보를 가까운 곳에서 찾으세요."),
        element("p", "", "원주시 공식 보건·복지 자료를 바탕으로 기관, 운영시간, 생활건강 정보를 알기 쉽게 연결합니다.")
      );
      const features = element("ul", "wonju-health-auth-features");
      for (const text of ["공식자료와 출처를 함께 확인", "큰 글자와 명확한 전화 연결", "응급·위기 상황은 안전 안내 우선"]) {
        const item = element("li");
        item.append(icon("cross"), element("span", "", text));
        features.append(item);
      }
      copy.append(features);
      const urgent = element("div", "wonju-health-auth-urgent");
      const urgentActions = element("div", "wonju-health-auth-urgent-actions");
      const emergency = element("a", "", "응급 119");
      emergency.href = "tel:119";
      emergency.setAttribute("aria-label", "응급 119 지금 전화하기");
      const suicide = element("a", "", "자살예방상담 109");
      suicide.href = "tel:109";
      suicide.setAttribute("aria-label", "자살예방상담 109 지금 전화하기");
      urgentActions.append(emergency, suicide);
      urgent.append(element("strong", "", "지금 긴급한 도움이 필요하다면"), urgentActions);
      story.append(copy, urgent);
      authPage.append(story);
    }
    document.querySelector("#wonju-health-service-header")?.remove();
    return true;
  }

  function ensureServiceHeader() {
    if (document.querySelector("#wonju-health-service-header")) return;
    const header = element("header", "wonju-health-service-header");
    header.id = "wonju-health-service-header";
    const brand = element("a", "wonju-health-header-brand");
    brand.href = "/";
    brand.setAttribute("aria-label", "원주시 생활건강 안내 홈으로 이동");
    brand.append(serviceLockup());

    const status = element("div", "wonju-health-trust-status");
    status.append(element("span", "wonju-health-status-dot"), element("span", "", "원주시 공식자료 연계"));

    const actions = element("nav", "wonju-health-header-actions");
    actions.setAttribute("aria-label", "서비스 메뉴");
    const newQuestion = element("button", "wonju-health-header-button wonju-health-header-new", "새 질문");
    newQuestion.type = "button";
    newQuestion.setAttribute("aria-label", "새 건강 질문 시작하기");
    newQuestion.prepend(icon("plus"));
    newQuestion.addEventListener("click", () => {
      const trigger = document.querySelector("#sidebar-new-chat-button");
      if (trigger) trigger.click();
      else window.location.assign("/");
    });
    const history = element("button", "wonju-health-header-button wonju-health-header-history", "지난 질문");
    history.type = "button";
    history.setAttribute("aria-label", "지난 건강 질문 찾기");
    history.prepend(icon("history"));
    history.addEventListener("click", () => {
      const trigger = document.querySelector("#sidebar-search-button");
      if (trigger) trigger.click();
      else showServiceNotice("지난 질문 메뉴를 열 수 없습니다. 화면을 새로고침한 뒤 다시 시도해 주세요.");
    });
    const account = element("button", "wonju-health-header-button wonju-health-header-account", "내 정보");
    account.type = "button";
    account.setAttribute("aria-label", "내 정보 열기");
    account.prepend(icon("user"));
    account.addEventListener("click", () => {
      const selectors = [
        "#sidebar-user-button",
        "#user-menu-button",
        "[data-testid='user-menu-button']",
        "button[aria-label*='사용자']",
        "button[aria-label*='프로필']",
        "button[aria-label*='계정']",
        "button[aria-label*='profile' i]",
        "button[aria-label*='account' i]"
      ];
      const explicit = selectors
        .map((selector) => document.querySelector(selector))
        .find((button) => button && !button.closest("#wonju-health-service-header"));
      const candidates = [...document.querySelectorAll("#sidebar button, .wonju-health-native-toolbar button")]
        .filter((button) => !button.closest("#wonju-health-service-header"));
      const trigger = explicit || candidates.find((button) => {
        const image = button.querySelector("img");
        const description = `${image?.alt || ""} ${button.getAttribute("aria-label") || ""} ${button.getAttribute("title") || ""}`;
        return Boolean(image) && /사용자|프로필|계정|avatar|profile|account|user/i.test(description);
      });
      if (trigger) trigger.click();
      else showServiceNotice("내 정보 메뉴를 열 수 없습니다. 화면을 새로고침한 뒤 다시 시도해 주세요.");
    });
    const mobileMenu = element("button", "wonju-health-header-button wonju-health-header-menu", "메뉴");
    mobileMenu.type = "button";
    mobileMenu.setAttribute("aria-label", "서비스 메뉴 열기");
    mobileMenu.setAttribute("aria-expanded", "false");
    mobileMenu.setAttribute("aria-controls", "wonju-health-mobile-menu");
    mobileMenu.prepend(icon("menu"));
    const mobilePanel = element("div", "wonju-health-mobile-menu");
    mobilePanel.id = "wonju-health-mobile-menu";
    mobilePanel.hidden = true;
    const mobileActions = [
      ["새 질문", "plus", newQuestion],
      ["지난 질문", "history", history],
      ["내 정보·로그아웃", "user", account]
    ];
    for (const [label, iconName, target] of mobileActions) {
      const button = element("button", "wonju-health-mobile-menu-item", label);
      button.type = "button";
      button.prepend(icon(iconName));
      button.addEventListener("click", () => {
        mobilePanel.hidden = true;
        mobileMenu.setAttribute("aria-expanded", "false");
        target.click();
      });
      mobilePanel.append(button);
    }
    const closeMobileMenu = () => {
      mobilePanel.hidden = true;
      mobileMenu.setAttribute("aria-expanded", "false");
    };
    mobileMenu.addEventListener("click", (event) => {
      event.stopPropagation();
      const willOpen = mobilePanel.hidden;
      mobilePanel.hidden = !willOpen;
      mobileMenu.setAttribute("aria-expanded", String(willOpen));
      if (willOpen) mobilePanel.querySelector("button")?.focus();
    });
    mobilePanel.addEventListener("click", (event) => event.stopPropagation());
    document.addEventListener("click", closeMobileMenu);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !mobilePanel.hidden) {
        closeMobileMenu();
        mobileMenu.focus();
      }
    });
    const emergency = element("a", "wonju-health-header-link is-emergency", "응급 119");
    emergency.href = "tel:119";
    actions.append(newQuestion, history, account, mobileMenu, emergency);
    header.append(brand, status, actions, mobilePanel);
    document.body.prepend(header);
  }

  function showServiceNotice(message) {
    let notice = document.querySelector("#wonju-health-service-notice");
    if (!notice) {
      notice = element("div", "wonju-health-service-notice");
      notice.id = "wonju-health-service-notice";
      notice.setAttribute("role", "status");
      notice.setAttribute("aria-live", "polite");
      document.body.append(notice);
    }
    notice.textContent = message;
    notice.classList.add("is-visible");
    window.clearTimeout(notice._wonjuDismissTimer);
    notice._wonjuDismissTimer = window.setTimeout(() => notice?.classList.remove("is-visible"), 5000);
  }

  function isAdminRoute() {
    return /^\/(?:admin|workspace)(?:\/|$)/.test(window.location.pathname);
  }

  function adminLink(label, href, iconName) {
    const link = element("a", "wonju-health-admin-link", label);
    link.href = href;
    link.prepend(icon(iconName));
    if (window.location.pathname.startsWith(href)) link.setAttribute("aria-current", "page");
    return link;
  }

  function ensureAdminHeader() {
    if (document.querySelector("#wonju-health-admin-header")) return;
    document.querySelector("#wonju-health-service-header")?.remove();
    const header = element("header", "wonju-health-service-header wonju-health-admin-header");
    header.id = "wonju-health-admin-header";
    const brand = element("a", "wonju-health-header-brand");
    brand.href = "/";
    brand.setAttribute("aria-label", "원주시 생활건강 안내 서비스로 이동");
    brand.append(serviceLockup(true));
    const context = element("div", "wonju-health-admin-context");
    context.append(
      element("strong", "", "관리자 센터"),
      element("span", "", "서비스 운영·권한·모델 관리")
    );
    const actions = element("nav", "wonju-health-admin-actions");
    actions.setAttribute("aria-label", "관리자 메뉴");
    actions.append(
      adminLink("사용자", "/admin/users", "user"),
      adminLink("환경 설정", "/admin/settings", "building"),
      adminLink("모델·도구", "/workspace", "cross"),
      adminLink("챗봇으로", "/", "external")
    );
    header.append(brand, context, actions);
    document.body.prepend(header);
  }

  function decorateAdminContent() {
    const appAnchor = document.querySelector(
      "#sidebar, #users-tabs-container, #admin-settings-tabs-container, #workspace-container"
    );
    let root = appAnchor;
    let shell = appAnchor;
    while (root?.parentElement && root.parentElement !== document.body) {
      root = root.parentElement;
      if (root.getBoundingClientRect().height > 0 && getComputedStyle(root).display !== "contents") {
        shell = root;
      }
    }
    root?.classList.add("wonju-health-admin-root");
    shell?.classList.add("wonju-health-admin-shell");
    document.querySelector("#users-tabs-container")?.classList.add("wonju-health-admin-tabs");
    document.querySelector("#admin-settings-tabs-container")?.classList.add("wonju-health-admin-tabs");
    document.querySelector("#workspace-container")?.classList.add("wonju-health-workspace-content");

    const routePart = window.location.pathname.split("/").filter(Boolean).at(-1) || "";
    document.querySelector(`#${CSS.escape(routePart)}`)?.classList.add("wonju-health-admin-current");

    for (const node of document.querySelectorAll("#sidebar *")) {
      if ((node.textContent || "").trim() !== "OI" || node.children.length) continue;
      node.textContent = "원주";
      node.classList.add("wonju-health-admin-sidebar-mark");
    }

    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const textNodes = [];
    while (walker.nextNode()) textNodes.push(walker.currentNode);
    for (const node of textNodes) {
      const parent = node.parentElement;
      if (!parent || parent.closest("script, style, #wonju-health-admin-header")) continue;
      if (/Open\s?WebUI/i.test(node.nodeValue || "")) {
        node.nodeValue = (node.nodeValue || "").replace(
          /Open\s?WebUI/gi,
          "원주시 생활건강 관리 서비스"
        );
      }
    }

    for (const node of document.querySelectorAll("body *")) {
      const text = (node.textContent || "").trim();
      if (!text.includes("새로운 버전")) continue;
      let shell = node;
      let fixedShell = null;
      while (shell && shell !== document.body) {
        if (["fixed", "absolute"].includes(getComputedStyle(shell).position)) {
          fixedShell = shell;
          break;
        }
        shell = shell.parentElement;
      }
      if (fixedShell) {
        fixedShell.classList.add("wonju-health-admin-stock-notice");
        break;
      }
    }
  }

  function ensureAdminExperience() {
    if (!isAdminRoute()) return false;
    document.body.classList.remove(
      "wonju-health-auth",
      "wonju-health-chat",
      "wonju-health-conversation",
      "wonju-health-developer"
    );
    document.body.classList.add("wonju-health-admin");
    ensureAdminHeader();
    decorateAdminContent();
    return true;
  }

  function markNativeShell() {
    document.querySelectorAll(
      "#chat-container [id^='model-selector-'][id$='-button'], #chat-container .wonju-health-model-trigger"
    ).forEach((button) => button.closest("nav")?.classList.add("wonju-health-native-toolbar"));

    const stockHome = document.querySelector(".stock-empty-home");
    stockHome?.classList.add("wonju-health-stock-suggestions");
    const labels = [...document.querySelectorAll("#chat-pane div, #chat-pane span")]
      .filter((node) => (node.textContent || "").trim() === "제안");
    for (const label of labels) {
      let candidate = label.parentElement;
      while (candidate && candidate.id !== "chat-pane") {
        if (candidate.querySelectorAll("button").length >= 2 && !candidate.querySelector("#chat-input")) {
          candidate.classList.add("wonju-health-stock-suggestions");
          break;
        }
        candidate = candidate.parentElement;
      }
    }

    const chatPane = document.querySelector("#chat-pane");
    chatPane?.querySelector(":scope > .flex.items-center.h-full")
      ?.classList.add("wonju-health-home-root");
    const composer = document.querySelector("#message-input-container");
    let composerArea = composer?.parentElement;
    while (composerArea && composerArea !== chatPane) {
      const classes = composerArea.classList;
      if (classes.contains("flex-col") && classes.contains("justify-center") && classes.contains("items-center")) break;
      composerArea = composerArea.parentElement;
    }
    if (composerArea && composerArea !== chatPane) {
      composerArea.classList.add("wonju-health-home-composer-area");
      const composerRow = composerArea.parentElement;
      composerRow?.classList.add("wonju-health-home-composer-row");
      composerRow?.parentElement?.classList.add("wonju-health-home-panel");
      for (const child of composerArea.children) {
        if (!child.contains(composer)) child.classList.add("wonju-health-native-home-title");
      }
      for (const child of composerRow?.children || []) {
        if (child !== composerArea && !child.contains(composer)) child.classList.add("wonju-health-native-home-title");
      }
    }
    const suggestionShell = document.querySelector(".wonju-health-stock-suggestions")?.parentElement;
    if (suggestionShell && suggestionShell.id !== "chat-pane") {
      suggestionShell.classList.add("wonju-health-stock-suggestion-shell");
    }
  }

  let checkedModelAccessToken = "";
  let modelAccessPending = false;

  async function updateDeveloperModelAccess() {
    if (modelAccessPending) return;
    modelAccessPending = true;
    try {
      const token = localStorage.getItem("token") || "";
      if (!token || token === checkedModelAccessToken) return;
      const response = await fetch("/api/v1/models", {
        headers: { Authorization: `Bearer ${token}` },
        credentials: "same-origin"
      });
      if (!response.ok) return;
      const payload = await response.json();
      const models = Array.isArray(payload?.data) ? payload.data : [];
      const hasDeveloperModel = models.some((model) => model?.id === "gemma-4-31b-nvfp4");
      document.body.classList.toggle("wonju-health-developer", hasDeveloperModel);
      document.querySelectorAll(".wonju-health-native-toolbar")
        .forEach((toolbar) => toolbar.classList.toggle("wonju-health-developer-toolbar", hasDeveloperModel));
      checkedModelAccessToken = token;
    } catch (_) {
      // A transient permissions lookup must not block the resident interface.
    } finally {
      modelAccessPending = false;
    }
  }

  function ensureWelcome() {
    const chatPane = document.querySelector("#chat-pane");
    const messages = document.querySelector("#messages-container");
    const host = messages || chatPane;
    if (!host) return;
    const hasConversation = Boolean(document.querySelector(".user-message, .chat-assistant"));
    document.body.classList.toggle("wonju-health-conversation", hasConversation);
    const existing = document.querySelector("#wonju-health-welcome");
    if (hasConversation) {
      messages?.classList.remove("wonju-health-empty-chat");
      chatPane?.classList.remove("wonju-health-empty-chat");
      existing?.remove();
      document.querySelector("#wonju-health-home-tools")?.remove();
      return;
    }
    host.classList.add("wonju-health-empty-chat");
    if (existing) return;

    const welcome = element("section", "wonju-health-welcome");
    welcome.id = "wonju-health-welcome";
    welcome.setAttribute("aria-labelledby", "wonju-health-welcome-title");
    const intro = element("div", "wonju-health-welcome-intro");
    intro.append(
      element("span", "wonju-health-kicker", "오늘의 생활건강 안내"),
      element("h1", "", "안녕하세요. 무엇을 도와드릴까요?"),
      element("p", "", "증상부터 가까운 병원·약국, 보건소 이용까지 편한 말로 물어보세요.")
    );
    intro.querySelector("h1").id = "wonju-health-welcome-title";
    const grid = element("div", "wonju-health-quick-grid");
    for (const question of QUICK_QUESTIONS) {
      const button = element("button", "wonju-health-quick-button");
      button.type = "button";
      button.setAttribute("aria-label", `${question.title}: ${question.description}`);
      const copy = element("span", "wonju-health-quick-copy");
      copy.append(element("strong", "", question.title), element("small", "", question.description));
      button.append(icon(question.icon), copy, element("span", "wonju-health-quick-arrow", "→"));
      button.addEventListener("click", () => setQuestionInput(question.prompt));
      grid.append(button);
    }
    const tip = element("div", "wonju-health-welcome-tip");
    tip.append(icon("pin"), element("span", "", "가까운 기관을 찾을 때 읍면동을 함께 적으면 더 정확하게 안내할 수 있어요."));
    const safety = element("div", "wonju-health-welcome-safety");
    safety.append(
      element("strong", "", "긴급한 상황인가요?"),
      element("span", "", "답변을 기다리지 말고 바로 연락하세요."),
      Object.assign(element("a", "", "응급 119"), { href: "tel:119" }),
      Object.assign(element("a", "", "자살예방상담 109"), { href: "tel:109" })
    );
    const tools = element("section", "wonju-health-home-tools");
    tools.id = "wonju-health-home-tools";
    tools.setAttribute("aria-label", "빠른 건강 질문");
    welcome.append(intro);
    tools.append(grid, tip, safety);
    const homePanel = document.querySelector(".wonju-health-home-panel");
    const composerRow = document.querySelector(".wonju-health-home-composer-row");
    if (homePanel && composerRow && composerRow.parentElement === homePanel) {
      homePanel.insertBefore(welcome, composerRow);
      composerRow.after(tools);
    } else {
      host.prepend(welcome);
      welcome.after(tools);
    }
  }

  function decorateGuidance() {
    for (const heading of document.querySelectorAll("h1, h2, h3, h4")) {
      const key = (heading.textContent || "").trim();
      const step = GUIDANCE_STEPS.get(key);
      if (!step) continue;
      heading.classList.add("wonju-health-step-heading");
      heading.dataset.step = step;
      let cursor = heading.nextElementSibling;
      let previous = null;
      while (cursor && !/^H[1-4]$/.test(cursor.tagName)) {
        cursor.classList.add("wonju-health-step-content");
        cursor.dataset.step = step;
        if (!previous) cursor.classList.add("is-first");
        previous = cursor;
        cursor = cursor.nextElementSibling;
      }
      previous?.classList.add("is-last");
    }

    for (const heading of document.querySelectorAll("h2, h3")) {
      if ((heading.textContent || "").trim() !== "출처") continue;
      if (heading.parentElement?.querySelector(".wonju-health-source-stack")) continue;
      heading.classList.add("wonju-health-evidence-heading");
      const note = heading.nextElementSibling;
      note?.classList.add("wonju-health-evidence-note");
      if (
        note
        && !note.classList.contains("wonju-health-evidence-empty")
        && (note.textContent || "").includes("제공된 근거에서 확인할 수 없습니다.")
      ) {
        note.classList.add("wonju-health-evidence-empty");
        note.setAttribute("aria-label", "공식자료 확인 상태");
        const row = element(note.matches("ul, ol") ? "li" : "div", "wonju-health-evidence-empty-row");
        const copy = element("div", "wonju-health-evidence-empty-copy");
        copy.append(
          element("strong", "", "확인된 공식자료가 없습니다"),
          element("span", "", "제공된 근거에서 확인할 수 없습니다.")
        );
        row.append(icon("info"), copy);
        note.replaceChildren(row);
      }
    }
  }

  function decorateNativeMessageActions() {
    const visibleActions = [
      { pattern: /복사|copy/i, name: "copy", label: "복사", aria: "답변 복사" },
      { pattern: /읽어주기|소리로|speak|read aloud/i, name: "listen", label: "소리로 듣기", aria: "답변 소리로 듣기" },
      { pattern: /좋은 응답|thumbs? up|like/i, name: "helpful", aria: "도움이 된 답변" },
      { pattern: /잘못된 응답|thumbs? down|dislike/i, name: "unhelpful", aria: "도움이 되지 않은 답변" }
    ];
    const hiddenPattern = /편집|답변 이어서|재생성|다시 생성|계속 생성|접기|저장|\bedit\b|continue response|regenerate|collapse|\bsave\b/i;

    for (const button of document.querySelectorAll(
      "#messages-container button, #messages-container [role='button'], #messages-container div[aria-label]"
    )) {
      const descriptor = `${button.getAttribute("aria-label") || ""} ${button.getAttribute("title") || ""} ${(button.textContent || "").trim()}`;
      if (hiddenPattern.test(descriptor) || button.id === "continue-response-button") {
        button.classList.add("wonju-health-native-action-hidden");
        continue;
      }
      const action = visibleActions.find((candidate) => candidate.pattern.test(descriptor));
      if (!action) continue;
      button.classList.add("wonju-health-message-action");
      button.dataset.wonjuAction = action.name;
      button.setAttribute("aria-label", action.aria);
      if (action.label && !button.querySelector(".wonju-health-action-label")) {
        button.append(element("span", "wonju-health-action-label", action.label));
      }
    }
  }

  function localizeMessageTimes() {
    const candidates = new Set();
    for (const name of document.querySelectorAll("#response-message-model-name")) {
      const container = name.parentElement?.parentElement || name.parentElement;
      if (!container) continue;
      candidates.add(container);
      container.querySelectorAll("span, time, div").forEach((node) => candidates.add(node));
    }
    for (const node of candidates) {
      if (node.children.length && node.querySelector("#response-message-model-name")) continue;
      const text = (node.textContent || "").trim();
      const match = text.match(/^(오늘|어제)\s+(\d{1,2}:\d{2})\s*(AM|PM)$/i);
      if (!match) continue;
      const period = match[3].toUpperCase() === "AM" ? "오전" : "오후";
      node.textContent = `${match[1]} ${period} ${match[2]}`;
      node.classList.add("wonju-health-localized-time");
    }
  }

  function decorateChatShell() {
    const chatInput = document.querySelector("#chat-input, textarea");
    if (!chatInput) return false;
    document.body.classList.add("wonju-health-chat");
    document.body.classList.remove("wonju-health-auth");
    ensureServiceHeader();

    const classMap = {
      "#chat-container": "wonju-health-chat-container",
      "#chat-pane": "wonju-health-chat-pane",
      "#messages-container": "wonju-health-messages",
      "#message-input-container": "wonju-health-composer",
      "#chat-input-container": "wonju-health-input-shell",
      "#chat-input": "wonju-health-chat-input",
      "#send-message-button": "wonju-health-send-button",
      "#voice-input-button": "wonju-health-voice-button",
      "#input-menu-button": "wonju-health-input-menu"
    };
    for (const [selector, className] of Object.entries(classMap)) {
      document.querySelectorAll(selector).forEach((node) => node.classList.add(className));
    }
    document.querySelectorAll("#sidebar").forEach((node) => node.classList.add("wonju-health-sidebar"));
    document.querySelector("#sidebar-new-chat-button")?.setAttribute("aria-label", "새 건강 질문 시작하기");
    document.querySelector("#sidebar-search-button")?.setAttribute("aria-label", "지난 질문 찾기");

    chatInput.setAttribute("placeholder", "증상, 동네, 찾는 기관을 편하게 적어주세요");
    chatInput.setAttribute("aria-label", "원주시 생활건강 질문 입력");
    document.querySelector("#voice-input-button")?.setAttribute("aria-label", "말로 질문하기");
    document.querySelector("#send-message-button")?.setAttribute("aria-label", "질문 보내기");

    document.querySelectorAll("#chat-pane button").forEach((button) => {
      if (
        !button.closest("#message-input-container")
        && button.classList.contains("rounded-full")
        && button.classList.contains("pointer-events-auto")
        && button.querySelector("svg")
      ) {
        button.classList.add("wonju-health-scroll-latest");
        button.setAttribute("aria-label", "최신 답변으로 이동");
        button.parentElement?.classList.add("wonju-health-scroll-latest-wrap");
      }
    });
    decorateNativeMessageActions();

    const composer = document.querySelector("#message-input-container");
    composer?.closest("form")?.classList.add("wonju-health-composer-form");
    composer?.querySelectorAll("button").forEach((button) => {
      if (!button.id && button.classList.contains("bg-black")) button.classList.add("wonju-health-voice-call");
    });
    if (composer && !composer.querySelector("#wonju-health-composer-note")) {
      const note = element("div", "wonju-health-composer-note", "AI 안내는 진단이 아닙니다 · 위급하면 119");
      note.id = "wonju-health-composer-note";
      composer.append(note);
    }

    for (const button of document.querySelectorAll("#chat-container button")) {
      const text = (button.textContent || "").trim();
      if (text.includes(BRAND) || text.includes("wonju-health-rag")) button.classList.add("wonju-health-model-trigger");
    }
    markNativeShell();
    updateDeveloperModelAccess();
    document.querySelectorAll(".chat-assistant").forEach((node) => node.classList.add("wonju-health-assistant-message"));
    document.querySelectorAll(".user-message").forEach((node) => node.classList.add("wonju-health-user-message"));
    document.querySelectorAll("#response-message-model-name").forEach((node) => {
      node.classList.add("wonju-health-response-name");
      if (!node.parentElement?.querySelector(".wonju-health-response-mark")) {
        const mark = element("span", "wonju-health-response-mark");
        mark.append(icon("brand"));
        node.parentElement?.insertBefore(mark, node);
      }
    });
    localizeMessageTimes();
    document.querySelectorAll(".assistant-message-profile-image, [data-testid='assistant-avatar']")
      .forEach((avatar) => avatar.classList.add("wonju-health-stock-avatar"));
    ensureWelcome();
    decorateGuidance();
    return true;
  }

  function applyBranding() {
    if (document.title !== BRAND) document.title = BRAND;
    document.documentElement.lang = "ko";
    document.documentElement.dataset.wonjuHealth = "ready";
    if (ensureAuthExperience()) return;
    if (ensureAdminExperience()) return;
    document.body.classList.remove("wonju-health-admin");
    document.querySelector("#wonju-health-admin-header")?.remove();
    decorateChatShell();
    for (const node of document.querySelectorAll("h1, h2, [data-testid='logo-title']")) {
      if ((node.textContent || "").trim() === "Open WebUI") node.textContent = BRAND;
    }
  }

  function renderAll() {
    applyBranding();
    document.querySelectorAll(META_SELECTOR).forEach(renderCards);
    liveMetadataShells().forEach(renderCards);
    decorateGuidance();
  }

  let queued = false;
  const observer = new MutationObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      renderAll();
    });
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      renderAll();
      observer.observe(document.body, { childList: true, characterData: true, subtree: true });
    }, { once: true });
  } else {
    renderAll();
    observer.observe(document.body, { childList: true, characterData: true, subtree: true });
  }
})();
