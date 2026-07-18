package com.pethats.item;

import com.pethats.PetHats;
import net.fabricmc.fabric.api.creativetab.v1.FabricCreativeModeTab;
import net.minecraft.core.Registry;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.core.registries.Registries;
import net.minecraft.network.chat.Component;
import net.minecraft.resources.Identifier;
import net.minecraft.resources.ResourceKey;
import net.minecraft.world.item.CreativeModeTab;
import net.minecraft.world.item.Item;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Rarity;

/** Registers the 8 Pet Hats items and the mod's creative inventory tab. */
public final class ModItems {

	// Craftable tier (5), increasing cost and buff.
	public static final HatItem LEATHER_CAP = register("leather_cap", Rarity.COMMON, HatStats.flat(2, 1));
	public static final HatItem IRON_HELM = register("iron_helm", Rarity.COMMON, HatStats.flat(6, 2));
	public static final HatItem GOLDEN_CROWN = register("golden_crown", Rarity.UNCOMMON, HatStats.flat(10, 4));
	public static final HatItem DIAMOND_CIRCLET = register("diamond_circlet", Rarity.RARE, HatStats.flat(16, 6));
	public static final HatItem NETHERITE_WARHELM = register("netherite_warhelm", Rarity.RARE, HatStats.flat(24, 10));

	// Loot-only (2).
	public static final HatItem CAPTAINS_HAT =
			register("captains_hat", Rarity.UNCOMMON, HatStats.flat(8, 3, 0, 0.20));
	public static final HatItem PIGLIN_WAR_MASK =
			register("piglin_war_mask", Rarity.RARE, HatStats.flat(20, 8, 4, 0));

	// The ultimate hat: multiplies stats and unlocks the pet inventory.
	public static final HatItem ENDER_CROWN =
			register("ender_crown", Rarity.EPIC, HatStats.multiplier(5.0, 5.0, true));

	public static final CreativeModeTab PET_HATS_TAB = Registry.register(
			BuiltInRegistries.CREATIVE_MODE_TAB,
			Identifier.fromNamespaceAndPath(PetHats.MOD_ID, "pet_hats"),
			FabricCreativeModeTab.builder()
					.title(Component.translatable("itemGroup.pet_hats.pet_hats"))
					.icon(() -> new ItemStack(ENDER_CROWN))
					.displayItems((parameters, output) -> {
						output.accept(LEATHER_CAP);
						output.accept(IRON_HELM);
						output.accept(GOLDEN_CROWN);
						output.accept(DIAMOND_CIRCLET);
						output.accept(NETHERITE_WARHELM);
						output.accept(CAPTAINS_HAT);
						output.accept(PIGLIN_WAR_MASK);
						output.accept(ENDER_CROWN);
					})
					.build());

	private ModItems() {
	}

	private static HatItem register(String path, Rarity rarity, HatStats stats) {
		Identifier id = Identifier.fromNamespaceAndPath(PetHats.MOD_ID, path);
		ResourceKey<Item> key = ResourceKey.create(Registries.ITEM, id);
		Item.Properties properties = new Item.Properties().rarity(rarity).setId(key);
		HatItem item = new HatItem(properties, stats);
		return Registry.register(BuiltInRegistries.ITEM, id, item);
	}

	/** Registers this class's fields; called once from {@link PetHats#onInitialize()}. */
	public static void init() {
	}
}
