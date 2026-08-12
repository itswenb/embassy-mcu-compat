mod cli;

use anyhow::{Result, bail};
use clap::Parser;
use cli::{Cli, Command, SourcesCommand};
use mcu_compat_gen::sources::update_sources;

fn main() -> Result<()> {
    let cli = Cli::parse();

    match cli.command {
        Command::Sources(args) => match args.command {
            SourcesCommand::Update(args) => update_sources(&args.lock, &args.cache_dir),
        },
        Command::Audit(_) => bail!("audit 尚未实现"),
        Command::Generate(_) => bail!("generate 尚未实现"),
    }
}
