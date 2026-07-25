(() => {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const flash = document.getElementById("flash");
  const show = (message, error = false) => {
    if (!flash) return;
    flash.textContent = message;
    flash.classList.toggle("error", error);
    flash.hidden = false;
    window.scrollTo({top: 0, behavior: "smooth"});
  };
  const request = async (url, options = {}) => {
    const headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf, ...(options.headers || {})};
    const response = await fetch(url, {...options, headers});
    const body = response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) throw new Error(body?.message || `HTTP ${response.status}`);
    return body;
  };
  const busy = async (button, task) => {
    button.disabled = true;
    try { return await task(); } finally { button.disabled = false; }
  };

  document.querySelectorAll("[data-service-action]").forEach(button => button.addEventListener("click", () => busy(button, async () => {
    const operation = await request(`/api/v1/services/${button.dataset.serviceId}/actions/${button.dataset.action}`, {
      method: "POST", headers: {"Idempotency-Key": crypto.randomUUID()}
    });
    show(`Operation ${operation.operation_id}: ${operation.status}`);
  }).catch(error => show(error.message, true))));

  document.getElementById("create-group-form")?.addEventListener("submit", event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button");
    busy(button, async () => {
      const data = new FormData(event.currentTarget);
      const group = await request("/api/v1/recovery-groups", {method:"POST", body:JSON.stringify({name:data.get("name"),description:data.get("description") || ""})});
      location.href = `/groups?group=${group.group_id}`;
    }).catch(error => show(error.message, true));
  });

  document.getElementById("edit-group-form")?.addEventListener("submit", event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button");
    busy(button, async () => {
      const data = new FormData(event.currentTarget);
      await request(`/api/v1/recovery-groups/${event.currentTarget.dataset.groupId}`, {
        method: "PATCH",
        body: JSON.stringify({name: data.get("name"), description: data.get("description") || ""})
      });
      location.reload();
    }).catch(error => show(error.message, true));
  });

  document.getElementById("members-form")?.addEventListener("submit", event => {
    event.preventDefault(); const button=event.currentTarget.querySelector("button");
    busy(button, async () => {
      const ids=[...event.currentTarget.querySelectorAll('input[name="member"]:checked')].map(node=>node.value);
      await request(`/api/v1/recovery-groups/${event.currentTarget.dataset.groupId}/members`,{method:"PUT",body:JSON.stringify({managed_service_ids:ids})});
      location.reload();
    }).catch(error=>show(error.message,true));
  });

  document.getElementById("dependencies-form")?.addEventListener("submit", event => {
    event.preventDefault(); const button=event.currentTarget.querySelector("button");
    busy(button,async()=>{
      const dependencies=JSON.parse(new FormData(event.currentTarget).get("dependencies") || "[]");
      await request(`/api/v1/recovery-groups/${event.currentTarget.dataset.groupId}/dependencies`,{method:"PUT",body:JSON.stringify({dependencies})});
      location.reload();
    }).catch(error=>show(error.message,true));
  });

  const probeForm = document.getElementById("probe-form");
  if (probeForm) {
    const serviceSelect = probeForm.querySelector('select[name="managed_service_id"]');
    const definitionField = probeForm.querySelector('textarea[name="definition"]');
    const mode = document.getElementById("probe-mode");
    const deleteButton = document.getElementById("delete-probe-button");
    const fallback = {kind:"scm", timeout_seconds:2, interval_seconds:3, deadline_seconds:60};
    const serialized = document.getElementById("probe-definitions")?.textContent || "[]";
    const definitions = new Map(JSON.parse(serialized).map(probe => [String(probe.managed_service_id), probe.definition]));

    const renderProbe = () => {
      const serviceId = serviceSelect.value;
      const explicit = definitions.get(serviceId);
      definitionField.value = JSON.stringify(explicit || fallback, null, 2);
      definitionField.dataset.managedServiceId = serviceId;
      mode.textContent = explicit
        ? `当前显示 ${explicit.kind.toUpperCase()} 已保存探针。`
        : "未保存显式探针；运行时使用 SCM fallback。";
      deleteButton.disabled = !explicit;
    };

    serviceSelect.addEventListener("change", renderProbe);
    renderProbe();

    probeForm.addEventListener("submit", event => {
      event.preventDefault();
      const button = event.currentTarget.querySelector('button[type="submit"]');
      busy(button, async () => {
        const serviceId = serviceSelect.value;
        if (!serviceId || definitionField.dataset.managedServiceId !== serviceId) {
          throw new Error("探针编辑内容与当前服务不匹配，请重新选择服务");
        }
        const definition = JSON.parse(definitionField.value);
        await request(`/api/v1/recovery-groups/${probeForm.dataset.groupId}/services/${encodeURIComponent(serviceId)}/probe`, {
          method:"PUT", body:JSON.stringify(definition)
        });
        window.location.reload();
      }).catch(error => show(error.message, true));
    });

    deleteButton.addEventListener("click", () => busy(deleteButton, async () => {
      const serviceId = serviceSelect.value;
      if (!serviceId || definitionField.dataset.managedServiceId !== serviceId) {
        throw new Error("探针编辑内容与当前服务不匹配，请重新选择服务");
      }
      await request(`/api/v1/recovery-groups/${probeForm.dataset.groupId}/services/${encodeURIComponent(serviceId)}/probe`, {method:"DELETE"});
      window.location.reload();
    }).catch(error => show(error.message, true)));
  }

  document.querySelectorAll("[data-group-command]").forEach(button=>button.addEventListener("click",()=>busy(button,async()=>{
    const command=button.dataset.groupCommand; const group=button.dataset.groupId;
    const suffix=command === "run" ? "runs" : command;
    const result=await request(`/api/v1/recovery-groups/${group}/${suffix}`,{method:"POST",body:command === "run" ? "{}" : undefined});
    if (command === "run") location.href=`/runs/${result.run_id}`; else location.reload();
  }).catch(error=>show(error.message,true))));

  document.querySelector("[data-run-retry]")?.addEventListener("click", event=>busy(event.currentTarget,async()=>{
    const run=await request(`/api/v1/recovery-runs/${event.currentTarget.dataset.runId}/retry`,{method:"POST",body:"{}"});
    location.href=`/runs/${run.run_id}`;
  }).catch(error=>show(error.message,true)));
})();
