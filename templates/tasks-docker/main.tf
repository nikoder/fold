terraform {
  required_providers {
    coder = {
      source  = "coder/coder"
      version = ">= 2.13"
    }
    docker = {
      source = "kreuzwerker/docker"
    }
  }
}

# This template requires a valid Docker socket
# However, you can reference our Kubernetes/VM
# example templates and adapt the Claude Code module
#
# See: https://registry.coder.com/templates
provider "docker" {}

# A `coder_ai_task` resource enables Tasks and associates
# the task with the coder_app that will act as an AI agent.
resource "coder_ai_task" "task" {
  count  = data.coder_workspace.me.start_count
  app_id = module.opencode[count.index].task_app_id
}

# You can read the task prompt from the `coder_task` data source.
data "coder_task" "me" {}

variable "forgejo_author_token" {
  type        = string
  sensitive   = true
  description = "Forgejo access token for the fold-alpha author agent"
}

variable "fold_ntfy_token" {
  type        = string
  sensitive   = true
  description = "ntfy token (write-only on topic fold) for the raise-hand channel"
}

module "git-clone" {
  count    = data.coder_workspace.me.start_count
  source   = "registry.coder.com/coder/git-clone/coder"
  version  = "2.0.3"
  agent_id = coder_agent.main.id
  url      = "https://fold-alpha:${var.forgejo_author_token}@git.fold.example/you/fold-playground.git"
  base_dir = "/home/coder/projects"
}

# OpenCode against the local llamaswap endpoint (OpenAI-compatible;
# auth token is injected by Caddy, so the api key is a placeholder).
module "opencode" {
  count               = data.coder_workspace.me.start_count
  source              = "registry.coder.com/coder-labs/opencode/coder"
  version             = "0.1.2"
  agent_id            = coder_agent.main.id
  # Start inside the checkout so OpenCode auto-loads AGENTS.md (the fold
  # contract) — per-task prompts then carry only the task itself.
  workdir             = "/home/coder/projects/fold-playground"
  order               = 999
  ai_prompt           = data.coder_task.me.prompt
  report_tasks        = true
  post_install_script = data.coder_parameter.setup_script.value
  config_json = jsonencode({
    "$schema" = "https://opencode.ai/config.json"
    provider = {
      llamaswap = {
        npm  = "@ai-sdk/openai-compatible"
        name = "llamaswap"
        options = {
          baseURL = "https://inference.fold.example/v1"
          apiKey  = "caddy-injected"
        }
        models = {
          "glm53-flash" = { name = "GLM 5.3 Flash" }
        }
      }
    }
    model = "llamaswap/glm53-flash"
  })
}

# We are using presets to set the prompts, image, and set up instructions
# See https://coder.com/docs/admin/templates/extending-templates/parameters#workspace-presets
data "coder_workspace_preset" "default" {
  name    = "Real World App: Angular + Django"
  default = true
  parameters = {
    "system_prompt" = <<-EOT
      -- Framing --
      You are a helpful assistant that can help with code. You are running inside a Coder Workspace and provide status updates to the user via Coder MCP. Stay on track, feel free to debug, but when the original plan fails, do not choose a different route/architecture without checking the user first.

      -- Tool Selection --
      - playwright: previewing your changes after you made them
        to confirm it worked as expected
	    -	desktop-commander - use only for commands that keep running
        (servers, dev watchers, GUI apps).
      -	Built-in tools - use for everything else:
       (file operations, git commands, builds & installs, one-off shell commands)

      Remember this decision rule:
      - Stays running? → desktop-commander
      - Finishes immediately? → built-in tools

      -- Context --
      There is an existing app and tmux dev server running on port 8000. Be sure to read it's CLAUDE.md (./realworld-django-rest-framework-angular/CLAUDE.md) to learn more about it.

      Since this app is for demo purposes and the user is previewing the homepage and subsequent pages, aim to make the first visual change/prototype very quickly so the user can preview it, then focus on backend or logic which can be a more involved, long-running architecture plan.

    EOT

    "setup_script"    = <<-EOT
    # Set up projects dir
    mkdir -p /home/coder/projects
    cd $HOME/projects

    # Packages: Install additional packages
    sudo apt-get update && sudo apt-get install -y tmux
    if ! command -v google-chrome >/dev/null 2>&1; then
      yes | npx playwright install chrome
    fi

    # MCP: Install and configure MCP Servers
    npm install -g @wonderwhy-er/desktop-commander
    claude mcp add playwright npx -- @playwright/mcp@latest --headless --isolated --no-sandbox
    claude mcp add desktop-commander desktop-commander

    # Repo: Clone and pull changes from the git repository
    if [ ! -d "realworld-django-rest-framework-angular" ]; then
      git clone https://github.com/coder-contrib/realworld-django-rest-framework-angular.git
    else
      cd realworld-django-rest-framework-angular
      git fetch
      # Check for uncommitted changes
      if git diff-index --quiet HEAD -- && \
        [ -z "$(git status --porcelain --untracked-files=no)" ] && \
        [ -z "$(git log --branches --not --remotes)" ]; then
        echo "Repo is clean. Pulling latest changes..."
        git pull
      else
        echo "Repo has uncommitted or unpushed changes. Skipping pull."
      fi

      cd ..
    fi

    # Initialize: Start the development server
    cd realworld-django-rest-framework-angular && ./start-dev.sh
    EOT
    "preview_port"    = "4200"
    "container_image" = "codercom/example-universal:ubuntu"
  }

  # Pre-builds is a Coder Premium
  # feature to speed up workspace creation
  #
  # see https://coder.com/docs/admin/templates/extending-templates/prebuilt-workspaces
  # prebuilds {
  #   instances = 1
  #   expiration_policy {
  #      ttl = 86400  # Time (in seconds) after which unclaimed prebuilds are expired (1 day)
  #  }
  # }
}

# Advanced parameters (these are all set via preset)
data "coder_parameter" "system_prompt" {
  name         = "system_prompt"
  display_name = "System Prompt"
  type         = "string"
  form_type    = "textarea"
  description  = "System prompt for the agent with generalized instructions"
  default      = "You are a coding agent. Complete the task, commit nothing, and report status."
  mutable      = false
}
data "coder_parameter" "setup_script" {
  name         = "setup_script"
  display_name = "Setup Script"
  type         = "string"
  form_type    = "textarea"
  description  = "Script to run before running the agent"
  default      = ""
  mutable      = false
}
data "coder_parameter" "container_image" {
  name         = "container_image"
  display_name = "Container Image"
  type         = "string"
  default      = "codercom/example-universal:ubuntu"
  mutable      = false
}
data "coder_parameter" "preview_port" {
  name         = "preview_port"
  display_name = "Preview Port"
  description  = "The port the web app is running to preview in Tasks"
  type         = "number"
  default      = "3000"
  mutable      = false
}

data "coder_provisioner" "me" {}
data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

resource "coder_agent" "main" {
  arch           = data.coder_provisioner.me.arch
  os             = "linux"
  startup_script = <<-EOT
    set -e
    # Prepare user home with default files on first start.
    if [ ! -f ~/.init_done ]; then
      cp -rT /etc/skel ~
      touch ~/.init_done
    fi
  EOT

  # fold: author-agent identity and Forgejo access for task work.
  # The token arrives as a sensitive template variable (set at push
  # time, stored server-side), never in this file.
  env = {
    FORGEJO_URL         = "https://git.fold.example"
    FORGEJO_REPO        = "you/fold-playground"
    FORGEJO_TOKEN       = var.forgejo_author_token
    NTFY_URL            = "https://ntfy.fold.example"
    NTFY_TOKEN          = var.fold_ntfy_token
    GIT_AUTHOR_NAME     = "fold-alpha"
    GIT_AUTHOR_EMAIL    = "fold-alpha@fold.example"
    GIT_COMMITTER_NAME  = "fold-alpha"
    GIT_COMMITTER_EMAIL = "fold-alpha@fold.example"
  }

  # The following metadata blocks are optional. They are used to display
  # information about your workspace in the dashboard. You can remove them
  # if you don't want to display any information.
  # For basic resources, you can use the `coder stat` command.
  # If you need more control, you can write your own script.
  metadata {
    display_name = "CPU Usage"
    key          = "0_cpu_usage"
    script       = "coder stat cpu"
    interval     = 10
    timeout      = 1
  }

  metadata {
    display_name = "RAM Usage"
    key          = "1_ram_usage"
    script       = "coder stat mem"
    interval     = 10
    timeout      = 1
  }

  metadata {
    display_name = "Home Disk"
    key          = "3_home_disk"
    script       = "coder stat disk --path $${HOME}"
    interval     = 60
    timeout      = 1
  }

  metadata {
    display_name = "CPU Usage (Host)"
    key          = "4_cpu_usage_host"
    script       = "coder stat cpu --host"
    interval     = 10
    timeout      = 1
  }

  metadata {
    display_name = "Memory Usage (Host)"
    key          = "5_mem_usage_host"
    script       = "coder stat mem --host"
    interval     = 10
    timeout      = 1
  }

  metadata {
    display_name = "Load Average (Host)"
    key          = "6_load_host"
    # get load avg scaled by number of cores
    script   = <<EOT
      echo "`cat /proc/loadavg | awk '{ print $1 }'` `nproc`" | awk '{ printf "%0.2f", $1/$2 }'
    EOT
    interval = 60
    timeout  = 1
  }

  metadata {
    display_name = "Swap Usage (Host)"
    key          = "7_swap_host"
    script       = <<EOT
      free -b | awk '/^Swap/ { printf("%.1f/%.1f", $3/1024.0/1024.0/1024.0, $2/1024.0/1024.0/1024.0) }'
    EOT
    interval     = 10
    timeout      = 1
  }
}

# See https://registry.coder.com/modules/coder/code-server
module "code-server" {
  count  = data.coder_workspace.me.start_count
  folder = "/home/coder/projects"
  source = "registry.coder.com/coder/code-server/coder"

  settings = {
    "workbench.colorTheme" : "Default Dark Modern"
  }

  # This ensures that the latest non-breaking version of the module gets downloaded, you can also pin the module version to prevent breaking changes in production.
  version = "~> 1.0"

  agent_id = coder_agent.main.id
  order    = 1
}

module "windsurf" {
  count    = data.coder_workspace.me.start_count
  source   = "registry.coder.com/coder/windsurf/coder"
  version  = "1.3.1"
  agent_id = coder_agent.main.id
}

module "cursor" {
  count    = data.coder_workspace.me.start_count
  source   = "registry.coder.com/coder/cursor/coder"
  version  = "1.4.1"
  agent_id = coder_agent.main.id
}

module "jetbrains" {
  count      = data.coder_workspace.me.start_count
  source     = "registry.coder.com/coder/jetbrains/coder"
  version    = "~> 1.0"
  agent_id   = coder_agent.main.id
  agent_name = "main"
  folder     = "/home/coder/projects"
}

resource "docker_volume" "home_volume" {
  name = "coder-${data.coder_workspace.me.id}-home"
  # Protect the volume from being deleted due to changes in attributes.
  lifecycle {
    ignore_changes = all
  }
  # Add labels in Docker to keep track of orphan resources.
  labels {
    label = "coder.owner"
    value = data.coder_workspace_owner.me.name
  }
  labels {
    label = "coder.owner_id"
    value = data.coder_workspace_owner.me.id
  }
  labels {
    label = "coder.workspace_id"
    value = data.coder_workspace.me.id
  }
  # This field becomes outdated if the workspace is renamed but can
  # be useful for debugging or cleaning out dangling volumes.
  labels {
    label = "coder.workspace_name_at_creation"
    value = data.coder_workspace.me.name
  }
}

resource "coder_app" "preview" {
  agent_id     = coder_agent.main.id
  slug         = "preview"
  display_name = "Preview your app"
  icon         = "${data.coder_workspace.me.access_url}/emojis/1f50e.png"
  url          = "http://localhost:${data.coder_parameter.preview_port.value}"
  share        = "authenticated"
  subdomain    = true
  open_in      = "tab"
  order        = 0
  healthcheck {
    url       = "http://localhost:${data.coder_parameter.preview_port.value}/"
    interval  = 5
    threshold = 15
  }
}

resource "docker_container" "workspace" {
  count = data.coder_workspace.me.start_count
  image = data.coder_parameter.container_image.value
  # Uses lower() to avoid Docker restriction on container names.
  name = "coder-${data.coder_workspace_owner.me.name}-${lower(data.coder_workspace.me.name)}"
  # Hostname makes the shell more user friendly: coder@my-workspace:~$
  hostname = data.coder_workspace.me.name
  user     = "coder"
  # Use the docker gateway if the access URL is 127.0.0.1
  entrypoint = ["sh", "-c", replace(coder_agent.main.init_script, "/localhost|127\\.0\\.0\\.1/", "host.docker.internal")]
  env        = ["CODER_AGENT_TOKEN=${coder_agent.main.token}"]
  host {
    host = "host.docker.internal"
    ip   = "host-gateway"
  }
  volumes {
    container_path = "/home/coder"
    volume_name    = docker_volume.home_volume.name
    read_only      = false
  }

  # Add labels in Docker to keep track of orphan resources.
  labels {
    label = "coder.owner"
    value = data.coder_workspace_owner.me.name
  }
  labels {
    label = "coder.owner_id"
    value = data.coder_workspace_owner.me.id
  }
  labels {
    label = "coder.workspace_id"
    value = data.coder_workspace.me.id
  }
  labels {
    label = "coder.workspace_name"
    value = data.coder_workspace.me.name
  }
}
