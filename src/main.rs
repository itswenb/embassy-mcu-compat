mod cli;

use anyhow::{Result, bail};
use clap::Parser;
use cli::{Cli, Command, SourcesCommand};
use mcu_compat_gen::report::run_audit;
use mcu_compat_gen::sources::update_sources;

fn main() -> Result<()> {
    let cli = Cli::parse();

    match cli.command {
        Command::Sources(args) => match args.command {
            SourcesCommand::Update(args) => update_sources(&args.lock, &args.cache_dir),
        },
        Command::Audit(args) => {
            let summary = run_audit(
                &args.lock,
                &args.cache_dir,
                &args.compat_dir,
                &args.output,
                args.frozen,
            )?;
            println!(
                "packs={} devices={} ready={} blocked={} unmapped={} not_applicable={}",
                summary.packs,
                summary.devices,
                summary.ready,
                summary.blocked,
                summary.unmapped,
                summary.not_applicable
            );
            Ok(())
        }
        Command::Generate(_) => bail!("generate 尚未实现"),
    }
}
