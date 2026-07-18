package com.pethats.menu;

import com.pethats.PetHats;
import net.fabricmc.fabric.api.menu.v1.ExtendedMenuType;
import net.minecraft.core.Registry;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.network.codec.ByteBufCodecs;
import net.minecraft.resources.Identifier;

/** Registers the pet inventory menu type, which needs the pet's entity id sent alongside the open-screen packet. */
public final class ModMenus {

	public static final ExtendedMenuType<PetInventoryMenu, Integer> PET_INVENTORY = Registry.register(
			BuiltInRegistries.MENU,
			Identifier.fromNamespaceAndPath(PetHats.MOD_ID, "pet_inventory"),
			new ExtendedMenuType<>(PetInventoryMenu::fromNetwork, ByteBufCodecs.VAR_INT));

	private ModMenus() {
	}

	public static void init() {
	}
}
