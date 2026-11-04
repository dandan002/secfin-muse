# Every system the project runs, symlinked into each root so they agree on names, ports and hostnames.
# Each runs as its own ECS service and reaches the internet through its own Cloudflare Tunnel (see edge):
# one level under the domain, as Cloudflare's free certificate covers *.<domain> only.
#
#   desk                    the LoanDesk platform's API at api.<domain>
#   web                     its web app at desk.<domain> (a second service in desk's system)
#   control ... analytics   the mocks: the external systems the desk connects to
#   chat                    Desk Chat, mock-ib's terminal UI (a second service in ib's system)

locals {
  catalog = {
    desk      = { port = 8100, host = "api" }
    control   = { port = 8000, host = "mock-control" }
    ib        = { port = 8010, host = "mock-ib" }
    gateway   = { port = 8020, host = "mock-gateway" }
    ngt       = { port = 8030, host = "mock-ngt" }
    wematch   = { port = 8040, host = "mock-wematch" }
    sfp       = { port = 8050, host = "mock-sfp" }
    custodian = { port = 8060, host = "mock-custodian" }
    analytics = { port = 8070, host = "mock-analytics" }
  }
  mocks = [for s in keys(local.catalog) : s if s != "desk"]

  # One tunnel per ECS service: a tunnel's connectors must all be able to serve every hostname on it.
  tunnels = merge(
    { for s, c in local.catalog : s => { hostname = "${c.host}.${var.domain_name}", port = c.port } },
    {
      web  = { hostname = "desk.${var.domain_name}", port = 80 }
      chat = { hostname = "mock-chat.${var.domain_name}", port = 80 }
    },
  )
  # Each system's static UI, served by nginx as a second ECS service with its own tunnel.
  uis = { desk = "web", ib = "chat" }
}
