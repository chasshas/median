#!/bin/sh
# One-step installer and updater for Median.
#
#   curl -fsSL https://raw.githubusercontent.com/chasshas/median/main/install.sh | sh
#   ./install.sh [options]            (from a clone)
#
# It checks Docker and Docker Compose, gets the code (clone or fast-forward pull), writes .env
# on the first run (random SECRET_KEY and database password, login password, host time zone,
# free port, deployment mode without the dev override), backs up the database when it updates
# a running install, builds the image, applies migrations, starts db, web and worker and waits
# for /healthz. Running it again updates the install; .env is never overwritten.
# Run ./install.sh --help for the options.

set -eu

REPO_URL=${MEDIAN_REPO:-https://github.com/chasshas/median.git}
BRANCH=${MEDIAN_BRANCH:-main}
INSTALL_DIR=${MEDIAN_DIR:-}
PORT=${MEDIAN_PORT:-}
TIMEZONE=${MEDIAN_TIMEZONE:-}
PASSWORD=${MEDIAN_PASSWORD:-}
NO_PASSWORD=0
ASSUME_YES=0
CONFIGURE_ONLY=0
SKIP_BACKUP=0
INSTALL_DOCKER=0

usage() {
	cat <<'EOF'
Usage: install.sh [options]

Installs Median with Docker Compose, or updates an existing install.

Options:
  --dir DIR          Install directory (default: the clone this script is in, else ~/median)
  --branch NAME      Git branch to install (default: main)
  --port PORT        Host port of the web UI (default: 8000, or the next free one)
  --timezone TZ      APP_TIMEZONE, an IANA name such as Europe/Berlin (default: the host's)
  --password PW      Login password (default: asked for, or generated with --yes)
  --no-password      No login (only for a machine nobody else can reach)
  --yes, -y          Do not ask questions; use defaults and generate a password
  --install-docker   Install Docker with get.docker.com if it is missing (Linux only)
  --skip-backup      Do not back up the database before updating a running install
  --configure-only   Get the code and write .env, then stop (no Docker needed)
  -h, --help         Show this help

The same settings can come from the environment: MEDIAN_DIR, MEDIAN_BRANCH, MEDIAN_PORT,
MEDIAN_TIMEZONE, MEDIAN_PASSWORD, MEDIAN_REPO.
EOF
}

say() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
	printf 'error: %s\n' "$*" >&2
	exit 1
}
have() { command -v "$1" >/dev/null 2>&1; }

while [ $# -gt 0 ]; do
	case $1 in
	--dir) [ $# -ge 2 ] || die "--dir needs a value"; INSTALL_DIR=$2; shift ;;
	--dir=*) INSTALL_DIR=${1#*=} ;;
	--branch) [ $# -ge 2 ] || die "--branch needs a value"; BRANCH=$2; shift ;;
	--branch=*) BRANCH=${1#*=} ;;
	--port) [ $# -ge 2 ] || die "--port needs a value"; PORT=$2; shift ;;
	--port=*) PORT=${1#*=} ;;
	--timezone) [ $# -ge 2 ] || die "--timezone needs a value"; TIMEZONE=$2; shift ;;
	--timezone=*) TIMEZONE=${1#*=} ;;
	--password) [ $# -ge 2 ] || die "--password needs a value"; PASSWORD=$2; shift ;;
	--password=*) PASSWORD=${1#*=} ;;
	--no-password) NO_PASSWORD=1 ;;
	-y | --yes) ASSUME_YES=1 ;;
	--install-docker) INSTALL_DOCKER=1 ;;
	--skip-backup) SKIP_BACKUP=1 ;;
	--configure-only) CONFIGURE_ONLY=1 ;;
	-h | --help) usage; exit 0 ;;
	*) usage >&2; die "unknown option: $1" ;;
	esac
	shift
done

# Questions go to the terminal even when the script itself comes through a pipe (curl | sh).
TTY=
if [ "$ASSUME_YES" = 0 ] && (: </dev/tty) 2>/dev/null; then
	TTY=/dev/tty
fi

ask() { # ask VAR "question" default
	if [ -n "$TTY" ]; then
		printf '%s [%s]: ' "$2" "$3" >"$TTY"
		IFS= read -r _answer <"$TTY" || _answer=
	else
		_answer=
	fi
	[ -n "$_answer" ] || _answer=$3
	eval "$1=\$_answer"
}

confirm() { # confirm "question" -> 0 for yes; without a terminal the answer is yes
	[ -n "$TTY" ] || return 0
	printf '%s [Y/n]: ' "$1" >"$TTY"
	IFS= read -r _answer <"$TTY" || _answer=
	case $_answer in [nN]*) return 1 ;; *) return 0 ;; esac
}

random_hex() { # random_hex BYTES
	if have openssl; then
		openssl rand -hex "$1"
	else
		od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'
	fi
}

random_password() { # 16 characters from an unambiguous alphabet
	LC_ALL=C tr -dc 'abcdefghijkmnpqrstuvwxyzACDEFGHJKLMNPQRSTUVWXYZ23456789' </dev/urandom |
		dd bs=16 count=1 2>/dev/null
}

# ---------------------------------------------------------------- code

script_dir=
case $0 in
*/install.sh) script_dir=$(cd "$(dirname "$0")" && pwd) ;;
esac

if [ -z "$INSTALL_DIR" ]; then
	if [ -n "$script_dir" ] && [ -f "$script_dir/docker-compose.yml" ] && [ -d "$script_dir/app" ]; then
		INSTALL_DIR=$script_dir
	else
		INSTALL_DIR=$HOME/median
	fi
fi

UPDATING=0
if [ -f "$INSTALL_DIR/docker-compose.yml" ] && [ -d "$INSTALL_DIR/app" ]; then
	say "Median found in $INSTALL_DIR"
	if [ -d "$INSTALL_DIR/.git" ] && have git; then
		if [ -n "$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=no)" ]; then
			warn "local changes in $INSTALL_DIR; not pulling (commit or stash them to update)"
		elif git -C "$INSTALL_DIR" symbolic-ref -q HEAD >/dev/null; then
			say "Updating the code (git pull --ff-only)"
			git -C "$INSTALL_DIR" pull --ff-only ||
				die "git pull failed; resolve it in $INSTALL_DIR and run the installer again"
		fi
	fi
	[ -f "$INSTALL_DIR/.env" ] && UPDATING=1
else
	if [ -e "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
		die "$INSTALL_DIR exists and is not a Median checkout; choose another --dir"
	fi
	have git || die "git is needed to download Median; install git and run this again"
	say "Downloading Median ($BRANCH) into $INSTALL_DIR"
	git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"
[ -f .env.example ] || die ".env.example is missing in $INSTALL_DIR"

# ---------------------------------------------------------------- Docker

DOCKER=docker
check_docker() {
	if ! have docker; then
		if [ "$INSTALL_DOCKER" = 1 ] && [ "$(uname -s)" = Linux ]; then
			have curl || die "curl is needed to install Docker"
			say "Installing Docker with get.docker.com"
			_tmp=$(mktemp)
			curl -fsSL https://get.docker.com -o "$_tmp"
			if [ "$(id -u)" = 0 ]; then sh "$_tmp"; else sudo sh "$_tmp"; fi
			rm -f "$_tmp"
		else
			die "Docker is not installed. Install Docker Engine (Linux) or Docker Desktop
(macOS, Windows with WSL 2): https://docs.docker.com/get-docker/
On Linux this script can do it for you: install.sh --install-docker"
		fi
	fi
	if ! docker info >/dev/null 2>&1; then
		if have sudo && sudo -n true 2>/dev/null && sudo docker info >/dev/null 2>&1; then
			DOCKER="sudo docker"
			warn "using sudo for Docker (add yourself to the docker group to avoid it)"
		elif have sudo && [ -n "$TTY" ] && sudo docker info >/dev/null 2>&1; then
			DOCKER="sudo docker"
			warn "using sudo for Docker (add yourself to the docker group to avoid it)"
		else
			die "cannot talk to the Docker daemon. Start Docker (sudo systemctl start docker,
or open Docker Desktop) and make sure your user may use it (sudo usermod -aG docker \$USER,
then log in again)"
		fi
	fi
	$DOCKER compose version >/dev/null 2>&1 ||
		die "the Docker Compose plugin is missing (docker compose). See https://docs.docker.com/compose/install/"
	$DOCKER compose up --help 2>/dev/null | grep -q -- '--wait' ||
		die "Docker Compose is too old (no 'up --wait'); update Docker"
}

if [ "$CONFIGURE_ONLY" = 0 ]; then
	check_docker
fi

compose() { $DOCKER compose "$@"; }

# ---------------------------------------------------------------- .env

port_in_use() {
	if have ss; then
		ss -Hltn "sport = :$1" 2>/dev/null | grep -q .
	elif have lsof; then
		lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
	else
		return 1
	fi
}

host_timezone() {
	_tz=
	if have timedatectl; then
		_tz=$(timedatectl show -p Timezone --value 2>/dev/null || true)
	fi
	if [ -z "$_tz" ] && [ -f /etc/timezone ]; then
		_tz=$(head -n 1 /etc/timezone)
	fi
	if [ -z "$_tz" ] && [ -L /etc/localtime ]; then
		_tz=$(readlink /etc/localtime | sed -n 's|.*zoneinfo/||p')
	fi
	case $_tz in
	'' | *[!A-Za-z0-9_/+-]*) _tz=UTC ;;
	esac
	printf '%s' "$_tz"
}

write_env() {
	# A pgdata volume from an earlier install keeps its password, so keep the default then.
	db_password=median
	volume_exists=0
	if [ "$CONFIGURE_ONLY" = 0 ] && $DOCKER volume inspect median_pgdata >/dev/null 2>&1; then
		volume_exists=1
	fi
	if [ "$volume_exists" = 0 ]; then
		db_password=$(random_hex 16)
	else
		warn "found the database volume of an earlier install; keeping its database password"
	fi

	if [ -z "$PORT" ]; then
		PORT=8000
		while port_in_use "$PORT" && [ "$PORT" -lt 8100 ]; do PORT=$((PORT + 1)); done
		ask PORT "Web port" "$PORT"
	fi
	case $PORT in '' | *[!0-9]*) die "invalid port: $PORT" ;; esac
	[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "invalid port: $PORT"

	if [ -z "$TIMEZONE" ]; then
		ask TIMEZONE "Time zone" "$(host_timezone)"
	fi
	case $TIMEZONE in '' | *[!A-Za-z0-9_/+-]*) die "invalid time zone: $TIMEZONE" ;; esac

	generated_password=0
	if [ "$NO_PASSWORD" = 1 ]; then
		PASSWORD=
	elif [ -z "$PASSWORD" ]; then
		if [ -n "$TTY" ]; then
			printf 'Login password (empty: generate one): ' >"$TTY"
			_stty=$(stty -g <"$TTY" 2>/dev/null || true)
			stty -echo <"$TTY" 2>/dev/null || true
			IFS= read -r PASSWORD <"$TTY" || PASSWORD=
			[ -n "$_stty" ] && stty "$_stty" <"$TTY" 2>/dev/null
			printf '\n' >"$TTY"
		fi
		if [ -z "$PASSWORD" ]; then
			PASSWORD=$(random_password)
			generated_password=1
		fi
	fi
	case $PASSWORD in *"'"* | *'
'*) die "the password must not contain a single quote or a line break" ;; esac

	secret_key=$(random_hex 32)
	tmp_env=$(mktemp .env.XXXXXX)
	# .env.example is the template, so new settings and their comments carry over. Values are
	# passed through the environment, never through the awk program text.
	ENV_DB_PASSWORD=$db_password ENV_PORT=$PORT ENV_TZ=$TIMEZONE \
		ENV_APP_PASSWORD=$PASSWORD ENV_SECRET_KEY=$secret_key \
		awk '
		BEGIN { FS = "="; pw = ENVIRON["ENV_DB_PASSWORD"] }
		/^DATABASE_URL=/ { print "DATABASE_URL=postgresql://median:" pw "@db:5432/median"; next }
		/^TEST_DATABASE_URL=/ { print "TEST_DATABASE_URL=postgresql://median:" pw "@db:5432/median_test"; next }
		/^POSTGRES_PASSWORD=/ { print "POSTGRES_PASSWORD=" pw; next }
		/^APP_TIMEZONE=/ { print "APP_TIMEZONE=" ENVIRON["ENV_TZ"]; next }
		/^APP_PASSWORD=/ { print "APP_PASSWORD='\''" ENVIRON["ENV_APP_PASSWORD"] "'\''"; next }
		/^SECRET_KEY=/ { print "SECRET_KEY=" ENVIRON["ENV_SECRET_KEY"]; next }
		/^WEB_PORT=/ { print "WEB_PORT=" ENVIRON["ENV_PORT"]; next }
		{ print }
		END {
			print ""
			print "# Written by install.sh: use docker-compose.yml only (no development override)."
			print "COMPOSE_FILE=docker-compose.yml"
		}
		' .env.example >"$tmp_env"
	chmod 600 "$tmp_env"
	mv "$tmp_env" .env
	say "Wrote $INSTALL_DIR/.env (mode 600)"
}

env_value() { # env_value KEY -> value from .env without surrounding quotes
	sed -n "s/^$1=//p" .env | tail -n 1 | sed "s/^'\\(.*\\)'\$/\\1/; s/^\"\\(.*\\)\"\$/\\1/"
}

if [ -f .env ]; then
	say "Keeping the existing .env"
	generated_password=0
	[ -n "$PORT" ] && warn "--port is ignored: .env exists (edit WEB_PORT there)"
	grep -q '^COMPOSE_FILE=' .env ||
		warn ".env has no COMPOSE_FILE line, so docker-compose.override.yml (development mode) is used; add COMPOSE_FILE=docker-compose.yml to .env for a deployment"
else
	write_env
fi

if [ "$CONFIGURE_ONLY" = 1 ]; then
	say "Configuration written; start Median with: cd $INSTALL_DIR && ./install.sh"
	exit 0
fi

# ---------------------------------------------------------------- start

if [ "$UPDATING" = 1 ] && [ "$SKIP_BACKUP" = 0 ] && [ -n "$(compose ps -q --status running web 2>/dev/null)" ]; then
	say "Backing up the database before the update (skip with --skip-backup)"
	compose exec -T web python -m app.cli backup ||
		die "the backup failed; fix it or run again with --skip-backup"
fi

say "Building the image (the first build takes a few minutes)"
compose build

say "Starting the database"
compose up -d --wait db

say "Applying migrations"
compose run --rm web python -m app.db.migrate

say "Starting web and worker"
compose up -d --remove-orphans

web_port=$(env_value WEB_PORT)
[ -n "$web_port" ] || web_port=8000
url="http://localhost:$web_port"

say "Waiting for $url/healthz"
healthy=0
i=0
while [ $i -lt 60 ]; do
	if have curl; then
		curl -fsS "$url/healthz" >/dev/null 2>&1 && healthy=1
	elif have wget; then
		wget -q -O /dev/null "$url/healthz" 2>/dev/null && healthy=1
	else
		compose exec -T web python -c \
			"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" \
			>/dev/null 2>&1 && healthy=1
	fi
	[ $healthy = 1 ] && break
	i=$((i + 1))
	sleep 2
done
if [ $healthy = 0 ]; then
	compose logs --no-color --tail=50 web >&2 || true
	die "Median did not answer on $url/healthz after 2 minutes (logs above; also: cd $INSTALL_DIR && docker compose logs)"
fi

app_password=$(env_value APP_PASSWORD)

cat <<EOF

Median is running: $url
EOF
if [ -z "$app_password" ]; then
	echo "Login:   none (APP_PASSWORD is empty; set it in .env before others can reach this machine)"
elif [ "$generated_password" = 1 ]; then
	echo "Login:   password $app_password  (generated; also in $INSTALL_DIR/.env as APP_PASSWORD)"
else
	echo "Login:   the password in $INSTALL_DIR/.env (APP_PASSWORD)"
fi
cat <<EOF
Folder:  $INSTALL_DIR
Logs:    cd $INSTALL_DIR && docker compose logs -f
Stop:    cd $INSTALL_DIR && docker compose down
Update:  cd $INSTALL_DIR && ./install.sh
Media and the database live in the Docker volumes median_media-data and median_pgdata.
EOF
