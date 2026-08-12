mod cli;

use anyhow::{Result, bail};
use clap::Parser;
use cli::{Cli, Command, SourcesCommand};

fn main() -> Result<()> {
    let cli = Cli::parse();

    match cli.command {
        Command::Sources(args) => match args.command {
            SourcesCommand::Update(_) => bail!("sources update 尚未实现"),
        },
        Command::Audit(_) => bail!("audit 尚未实现"),
        Command::Generate(_) => bail!("generate 尚未实现"),
    }
}
