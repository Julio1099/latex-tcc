# Fluxograma: avaliador de serviços públicos

Este fluxograma descreve a proposta atual do TCC: criar um mecanismo próprio para ambientes públicos digitais, em vez de adaptar diretamente um sistema já existente no mercado.

```mermaid
flowchart TD
    A["Usuário acessa página inicial, órgão público ou página de serviço"] --> B["Script cliente inicia sessão anônima ou identificada"]
    B --> C["API registra clique, página, serviço, origem e instante"]
    C --> D["API atualiza métricas da sessão: total de cliques, páginas visitadas e tempo"]
    D --> E{"Cliques ou tempo acima da faixa esperada?"}

    E -- "Não" --> F["Fluxo normal da navegação"]
    F --> C

    E -- "Sim" --> G["Registrar dificuldade silenciosamente para o admin"]
    G --> H{"Serviço é marcado como muito importante ou de alto tráfego?"}

    H -- "Sim" --> I["API retorna ação: exibir popup de sugestão"]
    I --> J{"Usuário aceita ir para o serviço sugerido?"}
    J -- "Sim" --> K["Frontend redireciona para página do serviço prioritário"]
    J -- "Não" --> L["Usuário continua navegação atual"]

    H -- "Não" --> M["API mantém navegação sem popup"]

    G --> N{"Usuário com dificuldade ou tempo acima da média?"}
    K --> N
    L --> N
    M --> N

    N -- "Não" --> O["Admin acompanha indicadores agregados"]
    N -- "Sim" --> P["API retorna ação: abrir página simples de feedback"]

    P --> Q["Usuário avalia por estrelas"]
    Q --> R["Usuário seleciona características: clareza, tempo, esforço, usabilidade, atendimento, eficácia"]
    R --> S["Usuário responde NPS: chance de recomendar o serviço"]
    S --> T["API salva feedback estruturado"]
    T --> U["Admin visualiza alertas, sessões críticas, notas, NPS e pontos de melhoria"]
    O --> U
```

## Decisões do fluxo

- O registro de cliques é contínuo e silencioso.
- Quando a faixa esperada é ultrapassada, a API registra uma dificuldade para análise administrativa.
- Para serviços comuns, a navegação continua sem interrupção.
- Para serviços de alto tráfego ou alta importância, a API retorna uma ação para o frontend exibir um popup de direcionamento.
- Para usuários com sinais de dificuldade, a API retorna uma ação para abrir uma página simples de feedback.
- O feedback combina três entradas: estrelas, características do problema e NPS.
