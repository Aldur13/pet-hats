package com.pethats.client;

import com.pethats.client.screen.PetInventoryScreen;
import com.pethats.menu.ModMenus;
import net.fabricmc.api.ClientModInitializer;
import net.minecraft.client.gui.screens.MenuScreens;

public final class PetHatsClient implements ClientModInitializer {

	@Override
	public void onInitializeClient() {
		MenuScreens.register(ModMenus.PET_INVENTORY, PetInventoryScreen::new);
	}
}
