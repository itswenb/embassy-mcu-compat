use std::env;
#[cfg(feature = "rt")]
use std::path::PathBuf;

const COMPAT_ENV: &str = "EMBASSY_MCU_COMPAT_CHIP";

include!("src/compat.rs");

#[derive(Debug, PartialEq, Eq)]
enum GetOneError {
    None,
    Multiple,
}

trait IteratorExt: Iterator {
    fn get_one(self) -> Result<Self::Item, GetOneError>;
}

impl<T: Iterator> IteratorExt for T {
    fn get_one(mut self) -> Result<Self::Item, GetOneError> {
        match self.next() {
            None => Err(GetOneError::None),
            Some(result) => match self.next() {
                Some(_) => Err(GetOneError::Multiple),
                None => Ok(result),
            },
        }
    }
}

fn chip_core_name(variables: impl IntoIterator<Item = String>) -> Result<String, GetOneError> {
    variables
        .into_iter()
        .filter(|name| name.starts_with("CARGO_FEATURE_STM32"))
        .get_one()
        .map(|name| {
            name.strip_prefix("CARGO_FEATURE_")
                .unwrap()
                .to_ascii_lowercase()
                .replace('_', "-")
        })
}

fn select_chip(alias: &str, requested: Option<&str>) -> Result<String, String> {
    let Some(requested) = requested else {
        return Ok(alias.to_owned());
    };
    if RISCV_CHIPS
        .iter()
        .any(|chip| chip.eq_ignore_ascii_case(requested))
    {
        return Err(format!(
            "{COMPAT_ENV}={requested} 是 RISC-V 型号，不能通过 embassy-stm32 使用"
        ));
    }
    if ALL_CHIPS
        .iter()
        .any(|chip| chip.eq_ignore_ascii_case(requested))
    {
        return Ok(requested.to_ascii_lowercase());
    }
    let available = ALL_CHIPS
        .iter()
        .filter(|chip| {
            !RISCV_CHIPS
                .iter()
                .any(|riscv| riscv.eq_ignore_ascii_case(chip))
        })
        .map(|chip| chip.to_ascii_lowercase())
        .collect::<Vec<_>>()
        .join(", ");
    Err(format!(
        "未知 {COMPAT_ENV}={requested}；可用真实型号：{available}"
    ))
}

fn chip_path(chip: &str, native: bool) -> String {
    if native {
        format!("../mcu-metapac/src/chips/{chip}")
    } else {
        format!("chips/{chip}")
    }
}

fn rerun_directive() -> String {
    format!("cargo:rerun-if-env-changed={COMPAT_ENV}")
}

fn main() {
    println!("{}", rerun_directive());

    #[cfg(feature = "rt")]
    let crate_dir = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());

    let alias = match chip_core_name(env::vars().map(|(name, _)| name)) {
        Ok(name) => name,
        Err(GetOneError::None) => panic!("No stm32xx Cargo feature enabled"),
        Err(GetOneError::Multiple) => panic!("Multiple stm32xx Cargo features enabled"),
    };
    let requested = match env::var(COMPAT_ENV) {
        Ok(value) => Some(value),
        Err(env::VarError::NotPresent) => None,
        Err(env::VarError::NotUnicode(_)) => panic!("{COMPAT_ENV} 不是 UTF-8"),
    };
    let chip = select_chip(&alias, requested.as_deref()).unwrap_or_else(|error| panic!("{error}"));
    let chip_path = chip_path(&chip, requested.is_some());

    #[cfg(feature = "rt")]
    println!(
        "cargo:rustc-link-search={}/src/{}",
        crate_dir.display(),
        chip_path,
    );

    println!("cargo:rustc-env=STM32_METAPAC_PAC_PATH={chip_path}/pac.rs");
    println!("cargo:rustc-env=STM32_METAPAC_METADATA_PATH={chip_path}/metadata.rs");
    println!("cargo:rerun-if-changed=build.rs");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_alias_is_selected_without_compat_variable() {
        assert_eq!(select_chip("stm32f103c8", None).unwrap(), "stm32f103c8");
    }

    #[test]
    fn compatible_chip_is_selected_for_matching_alias() {
        assert_eq!(
            select_chip("stm32f103c8", Some("gd32f103c8")).unwrap(),
            "gd32f103c8"
        );
    }

    #[test]
    fn unknown_chip_lists_available_real_chips() {
        let error = select_chip("stm32f103c8", Some("unknown32")).unwrap_err();
        assert!(error.contains("unknown32"));
        assert!(error.contains("gd32f103c8"));
    }

    #[test]
    fn real_chip_is_not_bound_to_stm32_feature() {
        assert_eq!(
            select_chip("stm32f103cb", Some("gd32f103c8")).unwrap(),
            "gd32f103c8"
        );
    }

    #[test]
    fn native_chip_uses_nested_generated_tree() {
        assert_eq!(
            chip_path("gd32f303cb", true),
            "../mcu-metapac/src/chips/gd32f303cb"
        );
        assert_eq!(chip_path("stm32f303cb", false), "chips/stm32f303cb");
    }

    #[test]
    fn zero_or_multiple_stm32_features_keep_upstream_errors() {
        assert_eq!(chip_core_name([]), Err(GetOneError::None));
        assert_eq!(
            chip_core_name([
                "CARGO_FEATURE_STM32F103C8".to_owned(),
                "CARGO_FEATURE_STM32F103CB".to_owned(),
            ]),
            Err(GetOneError::Multiple)
        );
    }

    #[test]
    fn compat_variable_is_a_build_rerun_input() {
        assert_eq!(
            rerun_directive(),
            "cargo:rerun-if-env-changed=EMBASSY_MCU_COMPAT_CHIP"
        );
    }
}
