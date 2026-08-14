use std::path::PathBuf;

use clap::{ArgGroup, Args, Parser, Subcommand};

#[derive(Debug, Parser)]
#[command(name = "mcu-compat-gen")]
#[command(about = "生成并审计 Embassy STM32 的厂商无关 MCU 兼容数据")]
pub struct Cli {
    #[command(subcommand)]
    pub command: Command,
}

#[derive(Debug, Subcommand)]
pub enum Command {
    /// 更新并锁定外部数据来源。
    Sources(SourcesArgs),
    /// 离线审计全部锁定器件。
    Audit(AuditArgs),
    /// 从官方基线生成兼容 stm32-metapac。
    Generate(GenerateArgs),
}

#[derive(Debug, Args)]
pub struct SourcesArgs {
    #[command(subcommand)]
    pub command: SourcesCommand,
}

#[derive(Debug, Subcommand)]
pub enum SourcesCommand {
    /// 联网更新索引、Pack 和目标数据库快照。
    Update(SourceUpdateArgs),
}

#[derive(Debug, Args)]
pub struct SourceUpdateArgs {
    /// 下载与解包缓存目录。
    #[arg(long, default_value = ".cache/sources")]
    pub cache_dir: PathBuf,
    /// 来源锁文件。
    #[arg(long, default_value = "sources.lock.toml")]
    pub lock: PathBuf,
    /// 原子更新到指定的 cmsis-rust-target-db commit。
    #[arg(long)]
    pub target_db_revision: Option<String>,
}

#[derive(Debug, Args)]
#[command(group(
    ArgGroup::new("audit_mode")
        .required(true)
        .multiple(false)
        .args(["frozen", "update_derived"])
))]
pub struct AuditArgs {
    /// 禁止接受任何来源漂移。
    #[arg(long)]
    pub frozen: bool,
    /// 仅在来源维护流程中更新派生审计报告。
    #[arg(long)]
    pub update_derived: bool,
    /// 下载与解包缓存目录。
    #[arg(long, default_value = ".cache/sources")]
    pub cache_dir: PathBuf,
    /// 来源锁文件。
    #[arg(long, default_value = "sources.lock.toml")]
    pub lock: PathBuf,
    /// 兼容映射目录。
    #[arg(long, default_value = "compat")]
    pub compat_dir: PathBuf,
    /// 审计报告输出文件。
    #[arg(long, default_value = "reports/inventory.json")]
    pub output: PathBuf,
}

#[derive(Debug, Args)]
pub struct GenerateArgs {
    /// 固定 revision 的官方 stm32-data-generated checkout。
    #[arg(long)]
    pub official_generated: PathBuf,
    /// 必须不存在或为空的输出目录。
    #[arg(long)]
    pub output: PathBuf,
    /// 下载与解包缓存目录。
    #[arg(long, default_value = ".cache/sources")]
    pub cache_dir: PathBuf,
    /// 来源锁文件。
    #[arg(long, default_value = "sources.lock.toml")]
    pub lock: PathBuf,
    /// 兼容映射目录。
    #[arg(long, default_value = "compat")]
    pub compat_dir: PathBuf,
    /// 由规范化 GD 事实生成的 Embassy 投影清单。
    #[arg(long)]
    pub projection_manifest: Option<PathBuf>,
    /// 投影引用的厂商原生 chip/register 数据目录。
    #[arg(long)]
    pub native_data: Option<PathBuf>,
    /// 投影生成的 Embassy 兼容 register 数据目录。
    #[arg(long)]
    pub projection_data: Option<PathBuf>,
    /// 仅用于端到端测试时包含 test 映射。
    #[arg(long)]
    pub include_test: bool,
}
