# API protótipo: avaliador de serviços públicos

Protótipo local para validar a ideia central do TCC: monitorar sinais de dificuldade na navegação de serviços públicos digitais e coletar feedback estruturado quando necessário.

## Executar

```powershell
cd C:\Users\Shadown\latex-tcc\prototype-api
python server.py
```

Servidor padrão:

```text
http://127.0.0.1:8080
```

Acesse a raiz do servidor no navegador para abrir o painel web do protótipo:

```text
http://127.0.0.1:8080/
```

Esse painel permite conferir o status da API, listar serviços, simular cliques, registrar tempo alto, abrir o feedback demo e visualizar dados administrativos.

## Protótipo visual estilo Figma

Além do painel administrativo, o servidor também entrega telas navegáveis para demonstrar a experiência do cidadão e a integração da API:

```text
http://127.0.0.1:8080/portal
```

Telas disponíveis:

- `/portal`: página pública simulada com estrutura densa de portal governamental, incluindo barra institucional, acessibilidade, login, busca, menus com submenus, destaques, notícias, carta de serviços, aplicações aninhadas e conteúdo recente;
- `/servico/imposto-renda`: página de serviço prioritário, usada para demonstrar popup de redirecionamento;
- `/servico/servico-publico-generico`: página de serviço comum;
- `/feedback?session_id=sessao-demo&service_id=imposto-renda`: formulário de feedback.

Nas telas do protótipo há um painel lateral chamado **API em tempo real**. Ele mostra:

- sessão anônima usada na navegação;
- requisição enviada para `/api/events/click` ou `/api/events/page-time`;
- resposta da API;
- ação tomada pela API, como fluxo normal, popup de sugestão ou abertura de feedback.

Regras atuais de demonstração:

- `pagina-inicial-governo`: excesso a partir de 13 cliques; recomenda o serviço prioritário `Imposto de Renda`;
- `imposto-renda`: excesso a partir de 11 cliques; serviço marcado como importante/alto tráfego;
- `servico-publico-generico`: excesso a partir de 13 cliques; abre feedback sem popup prioritário;
- tempo alto na página inicial também recomenda `Imposto de Renda` e abre feedback.

O banco SQLite é criado automaticamente em:

```text
prototype-api/avaliador.db
```

## Endpoints principais

### Saúde

```http
GET /health
```

### Registrar clique

```http
POST /api/events/click
Content-Type: application/json
```

Exemplo:

```json
{
  "session_id": "sessao-demo-1",
  "user_id": "usuario-opcional",
  "page_id": "home-gov",
  "page_type": "home",
  "service_id": "imposto-renda",
  "url": "https://www.gov.br/",
  "x": 420,
  "y": 220
}
```

Resposta esperada quando a faixa de cliques é ultrapassada em serviço importante:

```json
{
  "session_id": "sessao-demo-1",
  "service_id": "imposto-renda",
  "click_count_for_service": 6,
  "actions": [
    {
      "type": "suggest_redirect_popup",
      "reason": "high_click_count_on_important_service",
      "message": "Você parece estar procurando Imposto de Renda. Deseja ir direto para a página do serviço?",
      "target_service_id": "imposto-renda",
      "target_url": "https://www.gov.br/receitafederal/pt-br/assuntos/meu-imposto-de-renda"
    }
  ]
}
```

### Registrar tempo de página

```http
POST /api/events/page-time
Content-Type: application/json
```

Exemplo:

```json
{
  "session_id": "sessao-demo-1",
  "service_id": "imposto-renda",
  "page_id": "home-gov",
  "duration_seconds": 90
}
```

Se o tempo ficar acima do esperado, a API retorna:

```json
{
  "actions": [
    {
      "type": "open_feedback",
      "reason": "duration_above_expected",
      "url": "/feedback?session_id=sessao-demo-1&service_id=imposto-renda"
    }
  ]
}
```

### Página de feedback

```http
GET /feedback?session_id=sessao-demo-1&service_id=imposto-renda
```

A página coleta:

- avaliação por estrelas;
- características do problema;
- NPS de 0 a 10;
- comentário opcional.

### Enviar feedback via API

```http
POST /api/feedback
Content-Type: application/json
```

```json
{
  "session_id": "sessao-demo-1",
  "service_id": "imposto-renda",
  "stars": 2,
  "nps": 4,
  "traits": ["tempo_obtencao", "usabilidade"],
  "comment": "Não encontrei o caminho correto para declarar."
}
```

### Admin: alertas silenciosos

```http
GET /api/admin/alerts
```

### Admin: dashboard agregado

```http
GET /api/admin/dashboard
```

## Script de integração mínimo no frontend

```html
<script>
async function registrarClique(event) {
  const resposta = await fetch("http://127.0.0.1:8080/api/events/click", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: localStorage.getItem("session_id") || crypto.randomUUID(),
      page_id: document.body.dataset.pageId,
      page_type: document.body.dataset.pageType,
      service_id: document.body.dataset.serviceId,
      url: location.href,
      x: event.clientX,
      y: event.clientY
    })
  });

  const data = await resposta.json();
  localStorage.setItem("session_id", data.session_id);

  for (const action of data.actions || []) {
    if (action.type === "suggest_redirect_popup") {
      if (confirm(action.message)) location.href = action.target_url;
    }
    if (action.type === "open_feedback") {
      window.open("http://127.0.0.1:8080" + action.url, "_blank");
    }
  }
}

document.addEventListener("click", registrarClique);
</script>
```
